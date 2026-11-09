"""token / 会话"""
import base64
import datetime
import json
import re
import time

import requests

from .config import CHUNK, COOKIE_BASE, UA
from .cdp import wait_dom_ready


def _b64json(part):
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def decode_access_token(token):
    """解出 JWT 里的账号信息；失败返回 {}。"""
    try:
        claims = _b64json(token.split(".")[1])
    except Exception:
        return {}
    auth = claims.get("https://api.openai.com/auth") or {}
    prof = claims.get("https://api.openai.com/profile") or {}
    return {
        "user_id": auth.get("chatgpt_user_id") or claims.get("sub"),
        "account_id": auth.get("chatgpt_account_id"),
        "email": prof.get("email") or "",
        "name": prof.get("name") or "",
        "plan": auth.get("chatgpt_plan_type") or "",
        "exp": claims.get("exp"),
    }


def check_access_token(token, timeout=20):
    """调 ChatGPT 后端验证 accessToken，返回 (ok, info|错误信息)。"""
    try:
        r = requests.get(
            "https://chatgpt.com/backend-api/me",
            headers={"Authorization": "Bearer " + token, "User-Agent": UA,
                     "Accept": "application/json"},
            timeout=timeout,
            proxies={},
        )
    except Exception as exc:
        return False, "网络错误: %s" % exc
    if not r.ok:
        return False, "HTTP %d" % r.status_code
    try:
        me = r.json()
    except Exception:
        return False, "返回内容无法解析"
    info = decode_access_token(token)
    info.update({
        "email": me.get("email") or info.get("email"),
        "name": me.get("name") or info.get("name"),
        "phone": me.get("phone_number") or "",
        "country": me.get("country") or "",
    })
    return True, info


# --------------------------------------------------------------------------- #
# 浏览器会话 cookie
# --------------------------------------------------------------------------- #

def clear_session_cookies(cdp):
    for n in range(8):
        try:
            cdp.call("Network.deleteCookies", name="%s.%d" % (COOKIE_BASE, n),
                     domain=".chatgpt.com")
        except Exception:
            pass
    try:
        cdp.call("Network.deleteCookies", name=COOKIE_BASE, domain=".chatgpt.com")
    except Exception:
        pass


def set_session_cookies(cdp, session_token, expires=None):
    """按 NextAuth 分块规则写入会话 cookie，返回块数。"""
    if not session_token:
        raise ValueError("缺少 sessionToken")
    clear_session_cookies(cdp)
    exp = int(expires or (time.time() + 30 * 86400))
    chunks = [session_token[i:i + CHUNK] for i in range(0, len(session_token), CHUNK)]
    for n, piece in enumerate(chunks):
        res = cdp.call(
            "Network.setCookie",
            name="%s.%d" % (COOKIE_BASE, n),
            value=piece,
            domain=".chatgpt.com",
            path="/",
            secure=True,
            httpOnly=True,
            sameSite="Lax",
            expires=exp,
        )
        if not res.get("success"):
            raise RuntimeError("写入第 %d 块 cookie 失败" % n)
    return len(chunks)


def current_session_email(cdp):
    try:
        return cdp.js(
            "fetch('/api/auth/session',{credentials:'include',cache:'no-store'})"
            ".then(r=>r.text()).then(t=>{try{const j=JSON.parse(t);"
            "return (j.user&&j.user.email)||''}catch(e){return ''}}).catch(()=> '')"
        )
    except Exception:
        return ""


def login_account(cdp, account, timeout=40, url="https://chatgpt.com/"):
    """把账号会话写进浏览器并打开 url（默认首页），返回校验到的邮箱（失败为空串）。

    url 可直接给后续要去的页面（如定价页），省掉一次整页加载。
    """
    cd_exp = account.get("sessionExp") or account.get("expires")
    exp = int(cd_exp) if isinstance(cd_exp, (int, float)) else None
    if not exp:
        exp = int(time.time()) + 30 * 86400
    set_session_cookies(cdp, account["sessionToken"], exp)
    cdp.call("Page.navigate", url=url)
    # 不等 load 完成（图片/统计脚本拖尾，冷缓存时多 7s）：文档可用就开始校验会话
    wait_dom_ready(cdp)
    end = time.time() + timeout
    while time.time() < end:
        email = current_session_email(cdp)
        if email:
            return email
        time.sleep(0.5)
    return ""


# --------------------------------------------------------------------------- #
# 导入内容解析 / 时间
# --------------------------------------------------------------------------- #

def parse_session(raw):
    """
    解析用户粘贴的内容: 完整导出 JSON / 只有 accessToken 的 JSON / 裸 JWT。
    返回 (sessions:list[dict], 错误信息)
    """
    if not raw or not str(raw).strip():
        return [], "内容为空"
    text = str(raw).strip().lstrip("\ufeff")
    items = []

    def _push(obj):
        if isinstance(obj, str):
            tok = obj.strip()
            if tok.startswith("{") or tok.startswith("["):
                try:
                    _push(json.loads(tok))
                except Exception:
                    pass
            elif tok.count(".") == 2 and tok.startswith("eyJ"):
                items.append({"accessToken": tok})
        elif isinstance(obj, dict):
            if isinstance(obj.get("session"), dict):
                _push(obj["session"])
            at = obj.get("accessToken") or obj.get("access_token")
            st = (obj.get("sessionToken") or obj.get("session_token")
                  or obj.get("token"))
            if at:
                items.append({
                    "accessToken": at,
                    "sessionToken": st or "",
                    "expires": obj.get("expires") or "",
                })
        elif isinstance(obj, list):
            for x in obj:
                _push(x)

    try:
        _push(json.loads(text))
    except Exception:
        # 不是完整 JSON：按行/换行切分，尝试单独解析
        for line in re.split(r"[\r\n]+", text):
            line = line.strip().strip(",")
            if not line:
                continue
            try:
                _push(json.loads(line))
            except Exception:
                _push(line)

    if not items:
        return [], "没能识别出 accessToken，请粘贴完整的导出 JSON"
    # 去重
    seen, uniq = set(), []
    for it in items:
        at = it.get("accessToken")
        if at and at not in seen:
            seen.add(at)
            uniq.append(it)
    return uniq, ""


def iso_to_epoch(text):
    """把 2027-02-02T05:22:17.040Z 转成 epoch 秒；解析失败返回 None。"""
    if not text or not isinstance(text, str):
        return None
    try:
        txt = text.replace("Z", "+0000")
        fmt = "%Y-%m-%dT%H:%M:%S.%f%z" if "." in txt else "%Y-%m-%dT%H:%M:%S%z"
        return int(datetime.datetime.strptime(txt, fmt).timestamp())
    except Exception:
        return None


def nice_exp(ts):
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""
