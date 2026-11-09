"""payhub — 支付模块：支付宝 / 微信官方直连，后台配置，订单持久化。

设计对齐 proxyhub：
    data/pay.json       渠道配置 + 套餐价格（密钥字段加密落盘）
    data/orders.json    订单（{"orders":[...]}）

关键安全点：
    * 回调严格验签 + 金额必须与订单一致 + 订单幂等（已支付的忽略重复回调）
    * 没配渠道 / 没启用 → 下单直接拒绝，整个支付入口对外不可用（默认关闭）
    * 真正「发货/开通」不在本模块里自动触发，由上层在确认 paid 后决定
"""
import copy
import os
import secrets
import threading
import time

import crypto_store
from store import atomic_write_json, _read_json
from . import alipay, wechat

try:
    import segno
except Exception:
    segno = None


def qr_data_uri(text, scale=5):
    """把支付二维码串渲染成 PNG data URI（前端直接塞 <img src>）；segno 缺失时返回空串。"""
    if not text or segno is None:
        return ""
    try:
        import io
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="png", scale=scale, border=2)
        import base64
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("TD_DATA_DIR") or os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "pay.json")
ORDERS_PATH = os.path.join(DATA_DIR, "orders.json")

CHANNELS = ("alipay", "wechat")
ORDER_TTL = 30 * 60           # 未支付订单 30 分钟过期
_SECRET_FIELDS = {
    "alipay": ("appPrivateKey", "alipayPublicKey"),
    "wechat": ("apiV3Key", "privateKey", "platformCert"),
}

_lock = threading.RLock()
_cfg_cache = {"mtime": None, "data": None}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def _default_config():
    return {
        "version": 1,
        "notifyBase": "",
        "alipay": {"enabled": False, "appId": "", "gateway": alipay.DEFAULT_GATEWAY,
                   "appPrivateKey": "", "alipayPublicKey": ""},
        "wechat": {"enabled": False, "appId": "", "mchId": "", "serialNo": "",
                   "apiV3Key": "", "privateKey": "", "platformCert": ""},
        "products": [dict(p) for p in DEFAULT_PRODUCTS],
        "updatedAt": 0,
    }


# 首页套餐卡片：同一 title 的多个套餐合成一张卡，用 variant 切换（如 Pro 5X / 20X）
DEFAULT_PRODUCTS = [
    {"id": "plus", "title": "ChatGPT Plus", "variant": "", "plan": "plus", "price": 17900,
     "subtitle": "开启更多可能", "desc": "ChatGPT Plus 月度订阅 · 支付后自助充值",
     "bullets": ["高级智能，应对复杂工作", "生成更高质量的图像", "可跨应用执行操作的 Work 智能体",
                 "用 Codex 实现编程自动化", "更大的记忆容量和 20 GB 存储空间", "连接电子邮箱和日历"],
     "enabled": True, "recommend": False},
    {"id": "pro", "title": "ChatGPT Pro", "variant": "", "plan": "pro", "price": 84900,
     "subtitle": "更高阶的选择", "desc": "ChatGPT Pro 月度订阅",
     "bullets": ["Ultrafast speed", "我们最强大的前沿 Pro 模型", "Dot，你的全天候智能体",
                 "工作模式与 Codex 均享最快速度", "最高记忆容量和 100 GB 存储空间",
                 "抢先体验新工具和模型", "无广告"],
     "enabled": True, "recommend": True},
]
PRODUCT_TEXT_LIMITS = {"title": 40, "variant": 30, "subtitle": 60, "desc": 120}


def normalize_product(p):
    """补齐字段（兼容旧版只有 name 的套餐），不做合法性校验。"""
    p = dict(p or {})
    if not p.get("title"):
        p["title"] = p.get("name") or "ChatGPT %s" % (p.get("plan") or "").capitalize()
    p.setdefault("variant", "")
    p.setdefault("subtitle", "")
    p.setdefault("desc", "")
    bullets = p.get("bullets")
    if isinstance(bullets, str):
        bullets = bullets.splitlines()
    p["bullets"] = [str(b).strip() for b in (bullets or []) if str(b).strip()]
    p["enabled"] = p.get("enabled", True) is not False
    p["recommend"] = bool(p.get("recommend"))
    p["name"] = product_name(p)
    return p


def product_name(p):
    """订单 / 账单上展示的完整套餐名：标题 + 档位。"""
    return ("%s %s" % (p.get("title") or "", p.get("variant") or "")).strip()


def load_config():
    """解密后的配置副本（mtime 缓存）。"""
    with _lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = None
        if mtime != _cfg_cache["mtime"] or _cfg_cache["data"] is None:
            raw = _read_json(CONFIG_PATH, "products") if mtime else None
            cfg = _merge_defaults(raw)
            for ch, fields in _SECRET_FIELDS.items():
                for f in fields:
                    if cfg[ch].get(f):
                        cfg[ch][f] = crypto_store.decrypt(cfg[ch][f])
            _cfg_cache.update(mtime=mtime, data=cfg)
        return copy.deepcopy(_cfg_cache["data"])


def _merge_defaults(raw):
    cfg = _default_config()
    if isinstance(raw, dict):
        if isinstance(raw.get("notifyBase"), str):
            cfg["notifyBase"] = raw["notifyBase"]
        for ch in CHANNELS:
            if isinstance(raw.get(ch), dict):
                cfg[ch].update({k: v for k, v in raw[ch].items() if k in cfg[ch]})
        if isinstance(raw.get("products"), list) and raw["products"]:
            cfg["products"] = raw["products"]
    cfg["products"] = [normalize_product(p) for p in cfg["products"]]
    return cfg


def save_config(cfg):
    with _lock:
        out = copy.deepcopy(cfg)
        out["version"] = 1
        out["updatedAt"] = int(time.time())
        for ch, fields in _SECRET_FIELDS.items():
            for f in fields:
                if out.get(ch, {}).get(f):
                    out[ch][f] = crypto_store.encrypt(out[ch][f])
        atomic_write_json(CONFIG_PATH, out)
        _cfg_cache.update(mtime=None, data=None)


def channel_ready(cfg, channel):
    c = cfg.get(channel) or {}
    if not c.get("enabled"):
        return False
    if channel == "alipay":
        return all(c.get(k) for k in ("appId", "appPrivateKey", "alipayPublicKey"))
    return all(c.get(k) for k in ("appId", "mchId", "serialNo", "apiV3Key", "privateKey"))


def public_config(cfg=None):
    """给后台看的配置：密钥只给「是否已填」。"""
    cfg = cfg or load_config()
    def pub(ch, plain_fields, secret_fields):
        c = cfg[ch]
        out = {k: c.get(k, "") for k in plain_fields}
        out["enabled"] = bool(c.get("enabled"))
        out["ready"] = channel_ready(cfg, ch)
        for f in secret_fields:
            out["has_" + f] = bool(c.get(f))
        return out
    return {
        "notifyBase": cfg.get("notifyBase", ""),
        "alipay": pub("alipay", ("appId", "gateway"), _SECRET_FIELDS["alipay"]),
        "wechat": pub("wechat", ("appId", "mchId", "serialNo"), _SECRET_FIELDS["wechat"]),
        "products": cfg.get("products", []),
        "anyReady": channel_ready(cfg, "alipay") or channel_ready(cfg, "wechat"),
    }


def get_product(cfg, product_id):
    return next((p for p in cfg.get("products", []) if p.get("id") == product_id), None)


def public_products(cfg=None):
    """首页用：只给已上架的套餐，按后台顺序。"""
    cfg = cfg or load_config()
    out = []
    for p in cfg.get("products", []):
        if not p.get("enabled"):
            continue
        out.append({
            "id": p["id"], "title": p["title"], "variant": p.get("variant", ""),
            "name": p["name"], "plan": p["plan"],
            "priceYuan": "%.2f" % (int(p.get("price") or 0) / 100.0),
            "subtitle": p.get("subtitle", ""), "desc": p.get("desc", ""),
            "bullets": p.get("bullets", []), "recommend": bool(p.get("recommend")),
        })
    return out


# --------------------------------------------------------------------------- #
# 订单
# --------------------------------------------------------------------------- #
def _load_orders():
    return _read_json(ORDERS_PATH, "orders")


def _save_orders(data):
    atomic_write_json(ORDERS_PATH, data, compact=True)


def get_order(token):
    for o in _load_orders()["orders"]:
        if o.get("token") == token:
            return o
    return None


def get_order_by_out(out_trade_no):
    for o in _load_orders()["orders"]:
        if o.get("outTradeNo") == out_trade_no:
            return o
    return None


def update_order(token, **fields):
    """更新订单字段并落盘，返回更新后的订单（找不到返回 None）。"""
    with _lock:
        data = _load_orders()
        for o in data["orders"]:
            if o.get("token") == token:
                o.update(fields)
                _save_orders(data)
                return o
    return None


def list_orders(limit=200):
    orders = _load_orders()["orders"]
    orders.sort(key=lambda o: o.get("createdAt", 0), reverse=True)
    return orders[:limit]


def _put_order(order):
    with _lock:
        data = _load_orders()
        data["orders"] = [o for o in data["orders"] if o.get("id") != order["id"]]
        data["orders"].append(order)
        # 只保留最近 500 条，避免无限增长
        data["orders"].sort(key=lambda o: o.get("createdAt", 0), reverse=True)
        data["orders"] = data["orders"][:500]
        _save_orders(data)
    return order


def _notify_url(cfg, channel):
    base = (cfg.get("notifyBase") or "").rstrip("/")
    return "%s/api/pay/%s/notify" % (base, channel)


def _gen_out_trade_no():
    return "TD%s%s" % (time.strftime("%Y%m%d%H%M%S"), secrets.token_hex(4).upper())


def create_order(channel, product_id, email="", contact="", test=False):
    """建单并向支付渠道下单，返回 (order, result)。result={ok, qr|error}。"""
    cfg = load_config()
    if channel not in CHANNELS:
        return None, {"ok": False, "error": "支付方式不支持"}
    if not channel_ready(cfg, channel):
        return None, {"ok": False, "error": "该支付方式未配置或未启用"}
    if not cfg.get("notifyBase"):
        return None, {"ok": False, "error": "后台未配置回调域名（notifyBase）"}

    if test:
        amount, subject, plan, product_id = 1, "支付联调测试 ¥0.01", "", "test"
    else:
        product = get_product(cfg, product_id)
        if not product or not product.get("enabled"):
            return None, {"ok": False, "error": "套餐不存在或已下架"}
        amount = int(product.get("price") or 0)
        if amount < 1:
            return None, {"ok": False, "error": "套餐价格未设置"}
        subject = product_name(product) or "ChatGPT 订阅"
        plan = product.get("plan") or ""

    order = {
        "id": secrets.token_hex(8),
        "token": secrets.token_urlsafe(16),
        "outTradeNo": _gen_out_trade_no(),
        "channel": channel,
        "productId": product_id,
        "plan": plan,
        "amount": amount,
        "subject": subject,
        "email": (email or "").strip()[:120],
        "contact": (contact or "").strip()[:120],
        "status": "created",
        "test": bool(test),
        "providerTradeNo": "",
        "createdAt": int(time.time()),
        "expireAt": int(time.time()) + ORDER_TTL,
        "paidAt": None,
        "activateJobId": None,
        "logs": [],
    }
    mod = alipay if channel == "alipay" else wechat
    res = mod.create(cfg[channel], order["outTradeNo"], amount, subject, _notify_url(cfg, channel))
    if res.get("ok"):
        order["qr"] = res["qr"]
        order["logs"].append([int(time.time()), "已下单，等待扫码支付"])
    else:
        order["status"] = "failed"
        order["logs"].append([int(time.time()), "下单失败：" + res.get("error", "")])
    _put_order(order)
    return order, res


def _mark_paid(order, info, source):
    """幂等置为已支付：金额必须一致。返回 (changed, error)。"""
    with _lock:
        cur = get_order_by_out(order["outTradeNo"]) or order
        if cur.get("status") in ("paid", "activating", "done"):
            return False, ""          # 已处理过，忽略重复
        if int(info.get("amountCents") or 0) != int(cur.get("amount") or -1):
            cur.setdefault("logs", []).append([int(time.time()), "金额不符，拒绝：%s" % info.get("amountCents")])
            _put_order(cur)
            return False, "金额不符"
        cur["status"] = "paid"
        cur["paidAt"] = int(time.time())
        cur["providerTradeNo"] = info.get("tradeNo", "")
        cur.setdefault("logs", []).append([int(time.time()), "支付成功（%s）" % source])
        _put_order(cur)
    return True, ""


def query_and_sync(token):
    """主动查单兜底：notify 没到时客户端轮询会走到这里。返回最新 order。"""
    order = get_order(token)
    if not order or order.get("status") != "created":
        return order
    cfg = load_config()
    mod = alipay if order["channel"] == "alipay" else wechat
    res = mod.query(cfg[order["channel"]], order["outTradeNo"])
    if res.get("ok") and res.get("paid"):
        _mark_paid(order, res, "主动查单")
    elif order.get("expireAt") and time.time() > order["expireAt"]:
        with _lock:
            cur = get_order_by_out(order["outTradeNo"])
            if cur and cur.get("status") == "created":
                cur["status"] = "expired"
                _put_order(cur)
    return get_order(token)


def handle_notify(channel, headers, raw_body, form):
    """处理回调。alipay 用 form（表单 dict），wechat 用 headers+raw_body（JSON）。

    返回 (ok, ack)。ack 是要回给支付平台的响应体字符串。
    """
    cfg = load_config()
    if channel == "alipay":
        ok, info = alipay.verify_notify(cfg["alipay"], form or {})
        ack_ok, ack_fail = "success", "failure"
    else:
        ok, info = wechat.verify_and_decrypt(cfg["wechat"], headers or {}, raw_body or "")
        ack_ok = '{"code":"SUCCESS","message":"OK"}'
        ack_fail = '{"code":"FAIL","message":"FAIL"}'
    if not ok:
        return False, ack_fail
    if not info.get("paid"):
        return True, ack_ok           # 非成功状态（如关闭/退款通知），确认收到即可
    order = get_order_by_out(info.get("outTradeNo", ""))
    if not order:
        return True, ack_ok           # 未知订单，确认收到避免反复重推
    _mark_paid(order, info, channel + " 回调")
    return True, ack_ok
