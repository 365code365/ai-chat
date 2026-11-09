"""payhub/wechat.py — 微信支付 Native（扫码）直连，API v3。

文档：/v3/pay/transactions/native，商户私钥 SHA256-RSA 请求签名；
    回调用 APIv3 密钥 AES-256-GCM 解密，并用微信平台证书公钥验签。
    下单   → 返回 code_url，前端渲染二维码
    回调   → 验签 + 解密 + 校验金额 + 幂等
    主动查 → GET /v3/pay/transactions/out-trade-no/{no} 兜底
"""
import json
import secrets
import time
import urllib.request

from . import pkiutil

API_BASE = "https://api.mch.weixin.qq.com"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _auth_header(cfg, method, url_path, body):
    """构造 Authorization: WECHATPAY2-SHA256-RSA2048 ...（对 方法\nURL\n时间戳\n随机串\n报文体\n 签名）。"""
    priv = pkiutil.load_private_key(cfg["privateKey"])
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16).upper()
    message = "%s\n%s\n%s\n%s\n%s\n" % (method, url_path, ts, nonce, body)
    sign = pkiutil.rsa_sign(priv, message)
    return ('WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",signature="%s",'
            'timestamp="%s",serial_no="%s"' % (cfg["mchId"], nonce, sign, ts, cfg["serialNo"]))


def _call(cfg, method, url_path, payload=None):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""
    headers = {
        "Authorization": _auth_header(cfg, method, url_path, body),
        "Accept": "application/json",
        "User-Agent": "TokenDesk/1.0",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + url_path, method=method,
                                 data=body.encode("utf-8") if body else None, headers=headers)
    try:
        with _opener.open(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def create(cfg, out_trade_no, amount_cents, subject, notify_url):
    """Native 下单，返回 {ok, qr, error}。qr 为 code_url（weixin://...），前端渲染二维码。"""
    payload = {
        "appid": cfg["appId"],
        "mchid": cfg["mchId"],
        "description": subject,
        "out_trade_no": out_trade_no,
        "notify_url": notify_url,
        "amount": {"total": int(amount_cents), "currency": "CNY"},
    }
    try:
        status, raw = _call(cfg, "POST", "/v3/pay/transactions/native", payload)
    except Exception as exc:
        return {"ok": False, "error": "请求微信失败：%s" % str(exc)[:120]}
    data = json.loads(raw) if raw else {}
    if status == 200 and data.get("code_url"):
        return {"ok": True, "qr": data["code_url"]}
    return {"ok": False, "error": data.get("message") or raw[:150]}


def query(cfg, out_trade_no):
    """查询订单，返回 {ok, paid, tradeNo, amountCents, state}。"""
    path = "/v3/pay/transactions/out-trade-no/%s?mchid=%s" % (out_trade_no, cfg["mchId"])
    try:
        status, raw = _call(cfg, "GET", path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120]}
    data = json.loads(raw) if raw else {}
    if status != 200:
        return {"ok": True, "paid": False, "state": data.get("code") or "UNKNOWN"}
    state = data.get("trade_state")
    return {"ok": True, "paid": state == "SUCCESS",
            "tradeNo": data.get("transaction_id", ""),
            "amountCents": (data.get("amount") or {}).get("total", 0), "state": state}


def verify_and_decrypt(cfg, headers, body):
    """验证并解密回调。headers 为请求头 dict（大小写不敏感由调用方处理），body 为原始报文字符串。

    返回 (ok, 信息dict)。校验：平台证书验签（若已配） + AES-GCM 解密。
    """
    # 1) 验签（有平台证书才验；没配则跳过，靠后续主动查单兜底）
    cert = cfg.get("platformCert")
    if cert:
        ts = headers.get("Wechatpay-Timestamp", "")
        nonce = headers.get("Wechatpay-Nonce", "")
        sign = headers.get("Wechatpay-Signature", "")
        message = "%s\n%s\n%s\n" % (ts, nonce, body)
        try:
            pub = pkiutil.load_public_key(cert)
            if not pkiutil.rsa_verify(pub, message, sign):
                return False, {"error": "回调验签失败"}
        except Exception as exc:
            return False, {"error": "平台证书无效：%s" % str(exc)[:80]}
    # 2) 解密 resource
    try:
        data = json.loads(body)
        res = data.get("resource") or {}
        plain = pkiutil.aes_gcm_decrypt(cfg["apiV3Key"], res.get("nonce", ""),
                                        res.get("ciphertext", ""), res.get("associated_data", ""))
        info = json.loads(plain)
    except Exception as exc:
        return False, {"error": "回调解密失败：%s" % str(exc)[:80]}
    if info.get("appid") and info["appid"] != cfg.get("appId"):
        return False, {"error": "appid 不匹配"}
    return True, {
        "outTradeNo": info.get("out_trade_no", ""),
        "tradeNo": info.get("transaction_id", ""),
        "amountCents": (info.get("amount") or {}).get("total", 0),
        "paid": info.get("trade_state") == "SUCCESS",
        "verified": bool(cert),
    }
