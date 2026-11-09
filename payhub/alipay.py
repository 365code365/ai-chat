"""payhub/alipay.py — 支付宝「当面付 / 扫码支付」(precreate) 直连。

文档：开放平台 alipay.trade.precreate / query，RSA2 签名。
    下单   → 返回二维码串 qr_code，前端渲染成二维码让客户扫
    回调   → 异步 notify（表单），验签 + 校验金额 + 幂等
    主动查 → alipay.trade.query 兜底（notify 没来时）
"""
import json
import time
import urllib.parse
import urllib.request

from . import pkiutil

DEFAULT_GATEWAY = "https://openapi.alipay.com/gateway.do"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _yuan(cents):
    return "%d.%02d" % (int(cents) // 100, int(cents) % 100)


def _sign_string(params):
    """支付宝待签串：按 key 升序，拼 k=v&...，跳过空值和 sign/sign_type。"""
    items = [(k, v) for k, v in params.items()
             if v not in ("", None) and k not in ("sign", "sign_type")]
    items.sort()
    return "&".join("%s=%s" % (k, v) for k, v in items)


def _request(cfg, method, biz, extra=None):
    priv = pkiutil.load_private_key(cfg["appPrivateKey"])
    params = {
        "app_id": cfg["appId"],
        "method": method,
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
    }
    params.update(extra or {})
    params["sign"] = pkiutil.rsa_sign(priv, _sign_string(params))
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(cfg.get("gateway") or DEFAULT_GATEWAY, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    with _opener.open(req, timeout=20) as r:
        raw = r.read().decode("utf-8")
    data = json.loads(raw)
    key = method.replace(".", "_") + "_response"
    resp = data.get(key) or {}
    return resp, raw


def create(cfg, out_trade_no, amount_cents, subject, notify_url):
    """发起扫码下单，返回 {ok, qr, error}。qr 为二维码内容串。"""
    biz = {"out_trade_no": out_trade_no, "total_amount": _yuan(amount_cents), "subject": subject}
    try:
        resp, raw = _request(cfg, "alipay.trade.precreate", biz, {"notify_url": notify_url})
    except Exception as exc:
        return {"ok": False, "error": "请求支付宝失败：%s" % str(exc)[:120]}
    if resp.get("code") == "10000" and resp.get("qr_code"):
        return {"ok": True, "qr": resp["qr_code"]}
    return {"ok": False, "error": resp.get("sub_msg") or resp.get("msg") or raw[:150]}


def query(cfg, out_trade_no):
    """查询订单，返回 {ok, paid, tradeNo, amountCents, state}。"""
    try:
        resp, raw = _request(cfg, "alipay.trade.query", {"out_trade_no": out_trade_no})
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}
    if resp.get("code") != "10000":
        # 40004 + ACQ.TRADE_NOT_EXIST 表示还没扫码支付，不算错误
        return {"ok": True, "paid": False, "state": resp.get("sub_code") or "UNKNOWN"}
    status = resp.get("trade_status")
    cents = 0
    try:
        cents = round(float(resp.get("total_amount", "0")) * 100)
    except Exception:
        pass
    return {"ok": True, "paid": status in ("TRADE_SUCCESS", "TRADE_FINISHED"),
            "tradeNo": resp.get("trade_no", ""), "amountCents": cents, "state": status}


def verify_notify(cfg, form):
    """验证异步通知。form 为回调表单的 dict（值为字符串）。返回 (ok, 信息dict)。"""
    sign = form.get("sign", "")
    pub = pkiutil.load_public_key(cfg["alipayPublicKey"])
    # 待验串：除 sign/sign_type 外全部参数，按 key 升序
    items = sorted((k, v) for k, v in form.items()
                   if k not in ("sign", "sign_type") and v not in ("", None))
    to_verify = "&".join("%s=%s" % (k, v) for k, v in items)
    if not pkiutil.rsa_verify(pub, to_verify, sign):
        return False, {"error": "验签失败"}
    if form.get("app_id") and form["app_id"] != cfg.get("appId"):
        return False, {"error": "app_id 不匹配"}
    cents = 0
    try:
        cents = round(float(form.get("total_amount", "0")) * 100)
    except Exception:
        pass
    return True, {
        "outTradeNo": form.get("out_trade_no", ""),
        "tradeNo": form.get("trade_no", ""),
        "amountCents": cents,
        "paid": form.get("trade_status") in ("TRADE_SUCCESS", "TRADE_FINISHED"),
    }
