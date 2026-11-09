"""proxyhub — 代理 IP 模块：渠道配置、默认渠道、按浏览器分配动态住宅 IP。

概念
  渠道 channel   一个代理来源。kookeey（动态住宅 · 提取模式账密）或 custom（任意上游，
                 可用 {session} 占位符做粘性会话）。后台选一个为默认渠道，留空＝直连。
  槽位 slot      每个 Chrome 实例一个：管理端 "main"，客户池 "c0" "c1"…
                 每个槽位在中继上有独立端口，中继据此知道连接属于哪个浏览器。
  租约 lease     槽位当前用的「渠道 + session」。浏览器每次启动都签一份新租约
                 （新 session ⇒ 新住宅 IP），所以客户每笔开通都是一个新 IP，
                 任务内 session 不变、IP 保持一致；任务结束杀浏览器时释放。

存储
  data/proxy.json                 渠道与默认渠道（密码/token 加密落盘）
  data/proxy_leases/<slot>.json   租约；由占用该槽位的进程写，中继按 mtime 实时读取

换 IP（rotate）
  kookeey 填了开发者 token → 调 chgsessionip 接口给当前 session 换出口；
  否则 / 调用失败 → 换一个新 session。之后中继会断开该槽位的旧隧道，浏览器重连即走新 IP。
"""
import copy
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request

import crypto_store
from . import kookeey
from .tunnel import parse_upstream, probe, open_via, ssl_context  # noqa: F401  (re-export)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("TD_DATA_DIR") or os.path.join(HERE, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "proxy.json")
LEASE_DIR = os.path.join(DATA_DIR, "proxy_leases")

# 中继端口：管理端浏览器沿用 8898；客户池 c{i} 用 8960+i
RELAY_PORT = 8898
SLOT_BASE_PORT = 8960
SLOT_COUNT = max(16, int(os.environ.get("TD_BROWSER_POOL", "2") or 2))

CHANNEL_TYPES = ("kookeey", "custom")
ROTATIONS = ("task", "connection")       # task=每次启动浏览器/每笔任务一个新 IP；connection=每个连接换 IP
_SECRET_FIELDS = ("authPwd", "token", "upstream", "changeIpUrl")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

_lock = threading.RLock()
_cfg_cache = {"mtime": None, "data": None}
_lease_cache = {}          # slot -> (mtime, lease)
_fallback_sessions = {}    # 没有租约的槽位（中继进程内）临时 session


# --------------------------------------------------------------------------- #
# 槽位 / 端口
# --------------------------------------------------------------------------- #
def slot_port(slot):
    if slot == "main":
        return RELAY_PORT
    m = re.match(r"^c(\d+)$", slot or "")
    if not m:
        raise ValueError("未知槽位：%s" % slot)
    return SLOT_BASE_PORT + int(m.group(1))


def relay_slots():
    """中继要监听的 (端口, 槽位) 列表。"""
    return [(RELAY_PORT, "main")] + [(SLOT_BASE_PORT + i, "c%d" % i) for i in range(SLOT_COUNT)]


def new_session():
    """kookeey 要求 8 位随机串；用纯数字最稳。"""
    return "".join(secrets.choice("0123456789") for _ in range(8))


# --------------------------------------------------------------------------- #
# 文件读写
# --------------------------------------------------------------------------- #
def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# 渠道配置
# --------------------------------------------------------------------------- #
def _empty_config():
    return {"version": 2, "defaultId": "", "channels": [], "updatedAt": 0}


def _migrate(raw):
    """兼容旧版 {"upstream": "..."}：转成一个 custom 渠道并设为默认。"""
    if not isinstance(raw, dict):
        return _empty_config()
    if isinstance(raw.get("channels"), list):
        raw.setdefault("defaultId", "")
        return raw
    cfg = _empty_config()
    up = (raw.get("upstream") or "").strip()
    if up:
        cfg["channels"].append({
            "id": "legacy", "name": "旧配置", "type": "custom", "rotation": "task",
            "upstream": up, "changeIpUrl": "",
            "createdAt": raw.get("updatedAt") or 0, "updatedAt": raw.get("updatedAt") or 0,
        })
        cfg["defaultId"] = "legacy"
    return cfg


def load_config():
    """返回解密后的配置副本（mtime 缓存，中继每个连接都会调）。"""
    with _lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = None
        if mtime != _cfg_cache["mtime"] or _cfg_cache["data"] is None:
            cfg = _migrate(_read(CONFIG_PATH) if mtime else None)
            for ch in cfg["channels"]:
                for f in _SECRET_FIELDS:
                    if ch.get(f):
                        ch[f] = crypto_store.decrypt(ch[f])
            _cfg_cache.update(mtime=mtime, data=cfg)
        return copy.deepcopy(_cfg_cache["data"])


def save_config(cfg):
    with _lock:
        out = copy.deepcopy(cfg)
        out["version"] = 2
        out["updatedAt"] = int(time.time())
        out.pop("upstream", None)
        for ch in out["channels"]:
            for f in _SECRET_FIELDS:
                if ch.get(f):
                    ch[f] = crypto_store.encrypt(ch[f])
        _atomic_write(CONFIG_PATH, out)
        _cfg_cache.update(mtime=None, data=None)


def get_channel(cfg, ch_id):
    return next((c for c in cfg["channels"] if c.get("id") == ch_id), None) if ch_id else None


def default_channel(cfg=None):
    cfg = cfg or load_config()
    return get_channel(cfg, cfg.get("defaultId"))


def channel_upstream(ch, session=None):
    """渠道 + session → 上游 URL 字符串（交给 parse_upstream）。"""
    if not ch:
        return ""
    if ch["type"] == "kookeey":
        user, pwd = kookeey.build_auth(ch, session)
        return "%s://%s:%s@%s:%d" % (
            ch.get("protocol") or "http",
            urllib.parse.quote(user, safe=""), urllib.parse.quote(pwd, safe=""),
            ch.get("gateway") or kookeey.DEFAULT_GATEWAY, int(ch.get("port") or kookeey.DEFAULT_PORT))
    tpl = ch.get("upstream") or ""
    return tpl.replace("{session}", session or new_session()) if "{session}" in tpl else tpl


def uses_session(ch):
    """该渠道是否靠 session 区分 IP（决定能否「换 session 换 IP」）。"""
    if not ch:
        return False
    if ch["type"] == "kookeey":
        return True
    return "{session}" in (ch.get("upstream") or "")


def _clean_str(v, limit=300):
    return str(v or "").strip()[:limit]


def validate_channel(body, old=None):
    """把前端提交的数据整理成渠道；不合法抛 ValueError。密钥字段留空＝沿用旧值。"""
    old = old or {}
    ch_type = body.get("type") or old.get("type") or "kookeey"
    if ch_type not in CHANNEL_TYPES:
        raise ValueError("渠道类型只能是 kookeey / custom")
    rotation = body.get("rotation") or old.get("rotation") or "task"
    if rotation not in ROTATIONS:
        raise ValueError("轮换方式不对")
    now = int(time.time())
    ch = {
        "id": old.get("id") or secrets.token_hex(4),
        "name": _clean_str(body.get("name"), 40) or old.get("name") or
                ("kookeey 动态住宅" if ch_type == "kookeey" else "自定义代理"),
        "type": ch_type,
        "rotation": rotation,
        "createdAt": old.get("createdAt") or now,
        "updatedAt": now,
    }

    def secret(key):
        val = _clean_str(body.get(key), 600)
        return val if val else (old.get(key) or "")

    if ch_type == "kookeey":
        ch.update({
            "uid": re.sub(r"\D", "", _clean_str(body.get("uid"))),
            "accessid": re.sub(r"\D", "", _clean_str(body.get("accessid"))),
            "authName": _clean_str(body.get("authName"), 40),
            "authPwd": secret("authPwd"),
            "token": secret("token"),
            "geo": _clean_str(body.get("geo"), 80) or "US",
            "gateway": _clean_str(body.get("gateway"), 80) or kookeey.DEFAULT_GATEWAY,
            "port": int(body.get("port") or kookeey.DEFAULT_PORT),
            "protocol": body.get("protocol") if body.get("protocol") in ("http", "socks5") else "http",
        })
        if not ch["uid"]:
            raise ValueError("请填写 kookeey 用户 ID（数字）")
        if not ch["authName"] or not ch["authPwd"]:
            raise ValueError("请填写提取模式的账号和密码（可点「从 kookeey 拉取」）")
        if not re.match(r"^[A-Za-z0-9]+$", ch["authName"] + ch["authPwd"]):
            raise ValueError("kookeey 账号/密码只能是字母和数字")
        if not kookeey.GEO_RE.match(ch["geo"]):
            raise ValueError("地区码格式不对，例：US、JP、US_California、global")
        if not re.match(r"^[A-Za-z0-9.-]+$", ch["gateway"]):
            raise ValueError("网关地址不对")
        if not 1 <= ch["port"] <= 65535:
            raise ValueError("端口不对")
    else:
        ch["upstream"] = secret("upstream")
        ch["changeIpUrl"] = _clean_str(body.get("changeIpUrl"), 600) if "changeIpUrl" in body \
            else old.get("changeIpUrl", "")
        if not ch["upstream"]:
            raise ValueError("请填写上游代理地址")
        parse_upstream(ch["upstream"].replace("{session}", "12345678"))
        if ch["changeIpUrl"] and not re.match(r"^https?://", ch["changeIpUrl"]):
            raise ValueError("换 IP 链接需以 http(s):// 开头")
    return ch


def _mask_url(url):
    try:
        u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
        if u.password:
            return url.replace(":" + u.password + "@", ":••••@", 1)
        parts = url.split(":")
        if len(parts) == 4:
            return ":".join(parts[:3] + ["••••"])
    except Exception:
        pass
    return url


def public_channel(ch, cfg=None):
    """给前端看的渠道：密钥只给是否已填 / 打码。"""
    out = {k: ch.get(k) for k in ("id", "name", "type", "rotation", "createdAt", "updatedAt")}
    out["isDefault"] = bool(cfg and cfg.get("defaultId") == ch.get("id"))
    if ch["type"] == "kookeey":
        out.update({k: ch.get(k) for k in ("uid", "accessid", "authName", "geo",
                                           "gateway", "port", "protocol")})
        out["hasAuthPwd"] = bool(ch.get("authPwd"))
        out["hasToken"] = bool(ch.get("token"))
        out["summary"] = "%s:%s · %s · %s" % (ch.get("gateway"), ch.get("port"),
                                              ch.get("geo"), (ch.get("protocol") or "http").upper())
    else:
        out["upstreamMasked"] = _mask_url(ch.get("upstream") or "")
        out["hasChangeIpUrl"] = bool(ch.get("changeIpUrl"))
        out["summary"] = out["upstreamMasked"]
    out["usesSession"] = uses_session(ch)
    return out


# --------------------------------------------------------------------------- #
# 租约
# --------------------------------------------------------------------------- #
def _lease_path(slot):
    if not _SAFE_ID.match(slot or ""):
        raise ValueError("槽位名不合法")
    return os.path.join(LEASE_DIR, slot + ".json")


def load_lease(slot):
    path = _lease_path(slot)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _lease_cache.pop(slot, None)
        return None
    hit = _lease_cache.get(slot)
    if hit and hit[0] == mtime:
        return dict(hit[1])
    lease = _read(path)
    if not isinstance(lease, dict):
        return None
    _lease_cache[slot] = (mtime, lease)
    return dict(lease)


def _write_lease(lease):
    with _lock:
        _atomic_write(_lease_path(lease["slot"]), lease)
        _lease_cache.pop(lease["slot"], None)


def _make_lease(slot, ch, job_id=None, cdp_port=None, prev=None):
    session = new_session() if ch and ch.get("rotation") == "task" and uses_session(ch) else None
    return {
        "slot": slot,
        "channelId": ch["id"] if ch else "",
        "channelName": ch["name"] if ch else "直连",
        "rotation": ch.get("rotation") if ch else "",
        "session": session,
        "gen": time.time_ns(),
        "startedAt": int(time.time()),
        "jobId": job_id if job_id is not None else (prev or {}).get("jobId"),
        "cdpPort": cdp_port if cdp_port is not None else (prev or {}).get("cdpPort"),
        "exit": None,
    }


def new_lease(slot, job_id=None, cdp_port=None):
    """浏览器启动时调用：按默认渠道签一份新租约（新 session ⇒ 新 IP）。"""
    lease = _make_lease(slot, default_channel(), job_id, cdp_port)
    _write_lease(lease)
    return lease


def tag_lease(slot, **fields):
    """给租约补充信息（如 jobId），不改 session/gen。"""
    lease = load_lease(slot)
    if lease:
        lease.update(fields)
        _write_lease(lease)
    return lease


def release(slot):
    try:
        os.remove(_lease_path(slot))
    except OSError:
        pass
    _lease_cache.pop(slot, None)


def list_leases():
    out = []
    try:
        names = sorted(os.listdir(LEASE_DIR))
    except OSError:
        return out
    for n in names:
        if n.endswith(".json"):
            lease = load_lease(n[:-5])
            if lease:
                out.append(lease)
    return out


def lease_gen(slot):
    lease = load_lease(slot)
    return lease["gen"] if lease else 0


def record_exit(slot, gen, info):
    """把探测到的出口写回租约（只在 gen 没变时写，避免把旧 IP 记到新租约上）。"""
    with _lock:
        lease = load_lease(slot)
        if lease and lease.get("gen") == gen:
            lease["exit"] = info
            _write_lease(lease)


# --------------------------------------------------------------------------- #
# 中继用：槽位 → 上游
# --------------------------------------------------------------------------- #
def resolve(slot):
    """返回 (parse_upstream 结果或 None=直连, gen)。"""
    cfg = load_config()
    lease = load_lease(slot)
    if lease:
        ch = get_channel(cfg, lease.get("channelId"))
        if lease.get("channelId") and ch is None:
            ch = default_channel(cfg)          # 渠道被删了：沿用 session 切到默认渠道
        return parse_upstream(channel_upstream(ch, lease.get("session"))), lease["gen"]
    ch = default_channel(cfg)
    session = None
    if ch and ch.get("rotation") == "task" and uses_session(ch):
        session = _fallback_sessions.setdefault((slot, ch["id"]), new_session())
    return parse_upstream(channel_upstream(ch, session)), 0


# --------------------------------------------------------------------------- #
# 换 IP
# --------------------------------------------------------------------------- #
def _call_change_url(ch, session):
    url = ch["changeIpUrl"].replace("{session}", session or "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=ssl_context()))
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "TokenDesk/1.0"}),
                     timeout=15) as r:
        if r.status >= 400:
            raise RuntimeError("HTTP %d" % r.status)


def rotate(slot):
    """给槽位换出口 IP，返回 (新租约, 说明)。"""
    lease = load_lease(slot)
    if not lease:
        raise ValueError("该浏览器没有在用的代理租约（浏览器未启动？）")
    cfg = load_config()
    dflt = default_channel(cfg)
    ch = get_channel(cfg, lease.get("channelId"))
    if (lease.get("channelId") or "") != (cfg.get("defaultId") or "") or ch is None:
        new = _make_lease(slot, dflt, prev=lease)
        _write_lease(new)
        return new, "已切到默认渠道「%s」" % (dflt["name"] if dflt else "直连")
    if ch.get("rotation") == "connection":
        lease.update(gen=time.time_ns(), exit=None)
        _write_lease(lease)
        return lease, "该渠道每个连接都换 IP，已断开旧连接"

    note = ""
    session = lease.get("session")
    if ch["type"] == "kookeey" and ch.get("token") and session:
        try:
            kookeey.change_ip(ch, session)
            note = "已调用 kookeey 接口为 session %s 换 IP" % session
        except Exception as exc:
            session, note = new_session(), "接口换 IP 失败（%s），已改用新 session" % exc
    elif ch["type"] == "custom" and ch.get("changeIpUrl"):
        try:
            _call_change_url(ch, session)
            note = "已调用换 IP 链接"
        except Exception as exc:
            if not uses_session(ch):
                raise RuntimeError("换 IP 链接调用失败：%s" % exc)
            session, note = new_session(), "换 IP 链接失败（%s），已改用新 session" % exc
    elif uses_session(ch):
        session, note = new_session(), "已换新 session"
    else:
        raise ValueError("该渠道是固定出口，没有配置换 IP 链接")
    lease.update(session=session, gen=time.time_ns(), exit=None, rotatedAt=int(time.time()))
    _write_lease(lease)
    return lease, note


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def probe_slot(slot, timeout=15):
    """经中继该槽位端口探测：测到的就是该浏览器此刻的出口。"""
    gen = lease_gen(slot)
    info = probe({"scheme": "http", "host": "127.0.0.1", "port": slot_port(slot), "auth": None},
                 timeout=timeout)
    if info.get("ok") and gen:
        record_exit(slot, gen, info)
    return info


def probe_channel(ch, timeout=15):
    """不经中继，直接用该渠道的一个新 session 测一次。"""
    session = new_session() if uses_session(ch) and ch.get("rotation") == "task" else None
    info = probe(parse_upstream(channel_upstream(ch, session)), timeout=timeout)
    info["session"] = session
    return info
