"""admin_auth.py — 后台账号密码登录（PBKDF2 + 内存会话）。

首次启动自动生成密码，写入 data/auth.json 与 data/初始密码.txt，
也可以用环境变量覆盖：
    ADMIN_USER=admin ADMIN_PASSWORD=xxx python3 admin_server.py
"""
import hashlib
import hmac
import json
import os
import secrets
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
AUTH_PATH = os.path.join(DATA_DIR, "auth.json")
INIT_PW_PATH = os.path.join(DATA_DIR, "初始密码.txt")

ITERATIONS = 120000
SESSION_TTL = 12 * 3600
COOKIE_NAME = "td_session"

_sessions = {}


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()


def _write_private(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def ensure_credentials():
    """返回 {user, salt, hash}，不存在则创建。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(AUTH_PATH):
        try:
            with open(AUTH_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("user") and data.get("hash"):
                return data
        except Exception:
            pass

    user = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
    salt = secrets.token_hex(16)
    data = {"user": user, "salt": salt, "hash": _hash(password, salt),
            "createdAt": int(time.time())}
    _write_private(AUTH_PATH, json.dumps(data, ensure_ascii=False, indent=2))
    if not os.environ.get("ADMIN_PASSWORD"):
        _write_private(INIT_PW_PATH, "管理端登录\n\n用户名: %s\n密  码: %s\n\n"
                                    "改密码：删除 data/auth.json 后重启，或用 ADMIN_PASSWORD 环境变量。\n"
                                    % (user, password))
        print("[auth] 已生成管理端密码: %s / %s（也写入 data/初始密码.txt）" % (user, password))
    return data


def verify(user, password):
    data = ensure_credentials()
    if not user or not password:
        return False
    if not hmac.compare_digest(str(user), str(data["user"])):
        return False
    return hmac.compare_digest(_hash(password, data["salt"]), data["hash"])


def create_session(user):
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"user": user, "exp": time.time() + SESSION_TTL}
    return token, SESSION_TTL


def check_session(token):
    if not token:
        return None
    item = _sessions.get(token)
    if not item:
        return None
    if item["exp"] < time.time():
        _sessions.pop(token, None)
        return None
    return item["user"]


def revoke(token):
    _sessions.pop(token or "", None)


def current_user(cookie_header):
    """从 Cookie 头里取会话用户。"""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE_NAME:
            return check_session(v)
    return None
