"""proxyhub/kookeey.py — kookeey 动态住宅（提取模式 · 账密）对接。

文档：https://www.kookeey.com/apidoc
  * 连接串：用户ID-安全策略用户名:安全策略密码-国家ISO码[-session][-轮转间隔]@网关:1000
      - 不带 session  → 每个连接换一个 IP
      - 带 8 位 session → 粘性，同一 session 保持同一个 IP
  * 签名接口：GET https://www.kookeey.com/clientapi/<方法>?accessid=&signature=&<参数>&ts=
      signature = base64( hex( HMAC-SHA1(开发者token, "参数串&ts=…") ) )，参数顺序与 URL 一致
  * 主动换 IP：/clientapi/chgsessionip?uid=&token=md5(开发者token)&geo=&session=
"""
import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import urllib.request

from .tunnel import ssl_context

API_BASE = "https://www.kookeey.com/clientapi/"
DEFAULT_GATEWAY = "gate.kookeey.info"
DEFAULT_PORT = 1000

# 官方可选网关（按使用者所在区域优化）
GATEWAYS = [
    ("gate.kookeey.info", "默认网关"),
    ("gate-hk.kookeey.info", "东亚优化"),
    ("gate-jp.kookeey.info", "日本优化"),
    ("gate-sea.kookeey.info", "东南亚优化"),
    ("gate-us.kookeey.info", "美加优化"),
    ("gate-eu.kookeey.info", "欧洲优化"),
    ("gate-gb.kookeey.info", "英国优化"),
    ("gate-au.kookeey.info", "澳洲优化"),
    ("gate-in.kookeey.info", "南亚优化"),
    ("gate-th.kookeey.info", "泰国优化"),
]

# 国家/地区码：US、JP、US_California、US_California_city_LosAngeles、global
GEO_RE = re.compile(r"^[A-Za-z]{2}(_[A-Za-z0-9]+)*$|^global$")

# 直连调用 kookeey API，不走系统代理
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                      urllib.request.HTTPSHandler(context=ssl_context()))


class KookeeyError(RuntimeError):
    pass


def build_auth(ch, session=None):
    """返回 (用户名, 密码)。session=None 表示每个连接换 IP。"""
    user = "%s-%s" % (ch["uid"], ch["authName"])
    pwd = "%s-%s" % (ch["authPwd"], ch.get("geo") or "US")
    if session:
        pwd += "-" + session
    return user, pwd


def sign(token, query):
    """query 为按 URL 顺序拼好的「k=v&...&ts=...」。"""
    digest = hmac.new(token.encode("utf-8"), query.strip("&").encode("utf-8"),
                      hashlib.sha1).hexdigest()
    return base64.b64encode(digest.encode("utf-8")).decode()


def _get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "TokenDesk/1.0"})
    try:
        with _opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as exc:
        raise KookeeyError("请求 kookeey 失败：%s" % str(exc)[:100])
    try:
        data = json.loads(raw)
    except ValueError:
        raise KookeeyError("kookeey 返回了非 JSON：%s" % raw[:100])
    if not data.get("success"):
        raise KookeeyError("kookeey：%s（code %s）" % (data.get("msg") or "失败", data.get("code")))
    return data.get("data")


def call(ch, method, params=None):
    """调用签名接口。params 为有序 [(k, v), ...]。"""
    token = ch.get("token") or ""
    accessid = ch.get("accessid") or ch.get("uid") or ""
    if not token or not accessid:
        raise KookeeyError("没填开发者 ID / 开发者 token，无法调用 kookeey API")
    pairs = [(k, str(v)) for k, v in (params or [])] + [("ts", str(int(time.time())))]
    query = "&".join("%s=%s" % kv for kv in pairs)
    url = "%s%s?accessid=%s&signature=%s&%s" % (
        API_BASE, method, urllib.parse.quote(str(accessid)),
        urllib.parse.quote(sign(token, query), safe=""),
        urllib.parse.urlencode(pairs))
    return _get_json(url)


def fetch_auth(ch):
    """提取模式账密：{"authName", "authPwd"}。"""
    data = call(ch, "resauth") or {}
    return {"authName": data.get("authname") or "", "authPwd": data.get("authpwd") or ""}


def traffic(ch):
    """动态住宅套餐余量：剩余 / 总流量（GB）、套餐名、到期时间戳。"""
    data = call(ch, "package", [("t", 2)]) or {}
    pkg = data.get("package") or {}
    return {"leftGb": data.get("traffic_left"), "totalGb": data.get("traffic_total"),
            "package": pkg.get("name") or "", "expireAt": data.get("expire_time")}


def change_ip(ch, session):
    """调用 chgsessionip 让该 session 换一个出口 IP（session 不变）。"""
    token = ch.get("token") or ""
    if not token or not ch.get("uid"):
        raise KookeeyError("没填开发者 token，无法调 API 换 IP")
    q = urllib.parse.urlencode([
        ("uid", ch["uid"]),
        ("token", hashlib.md5(token.encode("utf-8")).hexdigest()),
        ("geo", ch.get("geo") or "US"),
        ("session", session),
    ])
    return _get_json(API_BASE + "chgsessionip?" + q)
