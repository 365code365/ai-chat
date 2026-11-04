#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cgpt.py — ChatGPT 会话 token 的底层能力（CDP 控制 + 登录注入 + 套餐弹窗）

被 admin_server.py（Web 管理端）和 login_chatgpt.py（命令行）共用。

要点:
  * sessionToken 长约 4KB，超过 Chrome 单 cookie 4096 字节上限，
    必须按 NextAuth 规则分块写入 __Secure-next-auth.session-token.0 / .1 ...
  * Chrome 在 DSH 沙箱里必须 --no-sandbox，否则渲染进程崩溃、调试端口不开。
  * 所有浏览器操作串行执行（cookie 是 profile 级的，同一时刻只能登录一个账号）。
"""
import base64
import datetime
import json
import os
import re
import subprocess
import time
import urllib.parse

import requests
import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
COOKIE_BASE = "__Secure-next-auth.session-token"
CHUNK = 3800
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

PLAN_LABEL = {
    "free": "免费版",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
}


# --------------------------------------------------------------------------- #
# 纯 HTTP 校验
# --------------------------------------------------------------------------- #
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
# CDP
# --------------------------------------------------------------------------- #
class CDP(object):
    def __init__(self, ws_url, timeout=120):
        self.ws = websocket.create_connection(
            ws_url, max_size=None, suppress_origin=True, timeout=timeout
        )
        self._id = 0

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expression, await_promise=True):
        r = self.call(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=await_promise,
            returnByValue=True,
        )
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError(res.get("description", "JS 执行失败"))
        return res.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _get(url, timeout=2):
    return requests.get(url, timeout=timeout, proxies={})


def debugger_ready(port, timeout=1.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = _get("http://127.0.0.1:%d/json/version" % port)
            if r.ok:
                return r.json()
        except Exception:
            pass
        time.sleep(0.3)
    return None


def launch_browser(port, profile, headless=False, log_path=None):
    cmd = [
        CHROME,
        "--no-sandbox",
        "--user-data-dir=%s" % profile,
        "--remote-debugging-port=%d" % port,
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,960",
    ]
    if headless:
        cmd += ["--headless=new", "--disable-gpu"]
    cmd.append("about:blank")
    log = open(log_path, "ab") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    if not debugger_ready(port, 45):
        raise RuntimeError("Chrome 调试端口 %d 未就绪（见 chrome.log）" % port)
    return proc


def open_tab(port, url="about:blank"):
    r = requests.put(
        "http://127.0.0.1:%d/json/new?%s" % (port, urllib.parse.quote(url, safe=":/?&=%#")),
        timeout=5,
        proxies={},
    )
    return r.json()


def list_pages(port):
    try:
        tabs = _get("http://127.0.0.1:%d/json/list" % port, timeout=3).json()
    except Exception:
        return []
    return [t for t in tabs if t.get("type") == "page"]


def find_chatgpt_tab(port):
    for t in list_pages(port):
        if t.get("url", "").startswith("https://chatgpt.com"):
            return t
    return None


def close_tab(port, tab_id):
    try:
        _get("http://127.0.0.1:%d/json/close/%s" % (port, tab_id), timeout=3)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 登录态
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


def wait_page_ready(cdp, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        try:
            state = cdp.js("document.readyState", await_promise=False)
            if state == "complete":
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def wait_for(cdp, expression, timeout=30, interval=1.0):
    """轮询一段返回真值的 JS 表达式。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cdp.js(expression):
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def current_session_email(cdp):
    try:
        return cdp.js(
            "fetch('/api/auth/session',{credentials:'include',cache:'no-store'})"
            ".then(r=>r.text()).then(t=>{try{const j=JSON.parse(t);"
            "return (j.user&&j.user.email)||''}catch(e){return ''}}).catch(()=> '')"
        )
    except Exception:
        return ""


def login_account(cdp, account, timeout=40):
    """把账号会话写进浏览器并打开首页，返回校验到的邮箱（失败为空串）。"""
    cd_exp = account.get("sessionExp") or account.get("expires")
    exp = int(cd_exp) if isinstance(cd_exp, (int, float)) else None
    if not exp:
        exp = int(time.time()) + 30 * 86400
    set_session_cookies(cdp, account["sessionToken"], exp)
    cdp.call("Page.navigate", url="https://chatgpt.com/")
    wait_page_ready(cdp)
    end = time.time() + timeout
    while time.time() < end:
        email = current_session_email(cdp)
        if email:
            return email
        time.sleep(1.5)
    return ""


# --------------------------------------------------------------------------- #
# 订阅页 / 套餐选择
# --------------------------------------------------------------------------- #
PRICING_URL = "https://chatgpt.com/pricing"

_RESOLVE_LINK_JS = r"""
(() => {
  const want = %s;
  const anchors = [...document.querySelectorAll('a[href*="highlight_plan="]')];
  const hit = anchors.find(a => {
    const href = a.getAttribute('href') || '';
    return href.indexOf('highlight_plan=' + want) >= 0 && /plus|pro/i.test(a.innerText || '');
  }) || anchors.find(a => (a.getAttribute('href') || '').indexOf('highlight_plan=' + want) >= 0);
  return hit ? hit.getAttribute('href') : '';
})()
"""

_DIALOG_JS = r"""
(() => {
  const plan = %s;
  const dlg = document.querySelector('[role="dialog"]');
  if (!dlg) return JSON.stringify({ dialog: false });
  const lines = dlg.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const lower = lines.map(s => s.toLowerCase());
  let idx = lower.indexOf(plan);
  if (idx < 0) idx = lower.findIndex(s => s === plan + ' 版' || s.indexOf(plan) === 0);
  let price = '', cta = '';
  if (idx >= 0) {
    for (let i = idx + 1; i < Math.min(lines.length, idx + 60); i++) {
      if (!price && /(JP¥|US\$|HK\$|NT\$|A\$|[$€£₱¥])\s?[\d.,]+/.test(lines[i])) price = lines[i];
      if (!cta && /升级至|升级到|切换至|切换到|已安排|当前套餐|立即购买|购买|订阅|包含|Upgrade|Continue|Buy/i.test(lines[i])) cta = lines[i];
      if (price && cta) break;
    }
  }
  return JSON.stringify({
    dialog: true,
    planFound: idx >= 0,
    price: price,
    cta: cta,
    title: (lines[0] || ''),
    snippet: lines.slice(0, 8)
  });
})()
"""


def resolve_plan_link(cdp, plan):
    """在定价页里找到「获取 Plus / 获取 Pro」按钮的真实链接。"""
    href = cdp.js(_RESOLVE_LINK_JS % json.dumps(plan))
    if href:
        return urllib.parse.urljoin("https://chatgpt.com", href)
    return ("https://chatgpt.com/?default_tab=personal&highlight_plan=%s#pricing" % plan)


def open_plan_dialog(cdp, plan, shot_path=None):
    """
    打开订阅弹窗并选中指定套餐。

    返回 {ok, url, price, cta, screenshot}
    """
    plan = (plan or "").lower()
    if plan not in ("plus", "pro"):
        raise ValueError("plan 只能是 plus 或 pro")

    cdp.call("Page.navigate", url=PRICING_URL)
    wait_page_ready(cdp)
    wait_for(cdp, "!!document.querySelector('a[href*=\"highlight_plan=\"]')", timeout=20)

    url = resolve_plan_link(cdp, plan)
    cdp.call("Page.navigate", url=url)
    wait_page_ready(cdp)

    ok = wait_for(cdp, "!!document.querySelector('[role=\"dialog\"]')", timeout=30, interval=0.8)
    info = {}
    if ok:
        try:
            raw = cdp.js(_DIALOG_JS % json.dumps(plan))
            info = json.loads(raw) if raw else {}
        except Exception:
            info = {}

    shot = None
    if shot_path:
        try:
            rect = None
            if ok:
                try:
                    raw_rect = cdp.js(
                        'JSON.stringify((()=>{const d=document.querySelector(\'[role="dialog"]\');'
                        'if(!d)return null;const r=d.getBoundingClientRect();'
                        'return {x:Math.round(r.x),y:Math.round(r.y),'
                        'width:Math.round(r.width),height:Math.round(r.height)}})())'
                    )
                    rect = json.loads(raw_rect) if raw_rect else None
                except Exception:
                    rect = None
            shot = capture(cdp, shot_path, rect)
        except Exception:
            shot = None

    return {
        "ok": bool(ok),
        "url": cdp.js("location.href", await_promise=False),
        "price": info.get("price", ""),
        "cta": info.get("cta", ""),
        "planFound": bool(info.get("planFound")),
        "screenshot": shot,
    }


def capture(cdp, path, rect=None):
    params = {"format": "png"}
    if rect and rect.get("width") and rect.get("height"):
        params["clip"] = {
            "x": max(0, rect["x"]),
            "y": max(0, rect["y"]),
            "width": rect["width"],
            "height": rect["height"],
            "scale": 2,
        }
    data = cdp.call("Page.captureScreenshot", **params)["data"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    return path


# --------------------------------------------------------------------------- #
# 结账页（Stripe）
# --------------------------------------------------------------------------- #
CHECKOUT_HINT = "/checkout/"
CARD_FRAME_HINT = "js.stripe.com"


def list_targets(port):
    try:
        return _get("http://127.0.0.1:%d/json/list" % port, timeout=3).json()
    except Exception:
        return []


def find_card_frame(port, timeout=45):
    """找到装着卡号/有效期/CVC 的 Stripe 子 frame，返回已连接的 CDP。"""
    end = time.time() + timeout
    while time.time() < end:
        for t in list_targets(port):
            url = t.get("url", "")
            if t.get("type") != "iframe" or CARD_FRAME_HINT not in url:
                continue
            if "elements-inner" not in url:
                continue
            try:
                cdp = CDP(t["webSocketDebuggerUrl"], timeout=20)
                if cdp.js("document.querySelectorAll('input[name=number]').length",
                          await_promise=False):
                    return cdp
                cdp.close()
            except Exception:
                continue
        time.sleep(1)
    return None


_CARD_FIELD_JS = r"""
(() => {
  const el = document.querySelector('input[name=%s]');
  if (!el) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(el, '');
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.focus();
  return true;
})()
"""


def _type_field(cdp, name, text):
    ok = cdp.js(_CARD_FIELD_JS % json.dumps(name), await_promise=False)
    if not ok:
        raise RuntimeError("结账页找不到字段: %s" % name)
    time.sleep(0.12)
    cdp.call("Input.insertText", text=text)
    time.sleep(0.2)


def digits(s):
    return re.sub(r"\D", "", s or "")


def fill_card(port, number, exp, cvc, timeout=45):
    """把卡信息填进 Stripe 表单，返回回读到的值。"""
    num = digits(number)
    expd = digits(exp)
    cvc_d = digits(cvc)
    if len(num) < 12:
        raise ValueError("卡号位数不对")
    if len(expd) not in (4, 6):
        raise ValueError("有效期格式应为 MM/YY")
    if len(cvc_d) not in (3, 4):
        raise ValueError("安全码应为 3-4 位")

    cdp = find_card_frame(port, timeout=timeout)
    if cdp is None:
        raise RuntimeError("没有找到 Stripe 卡信息输入框，结账页可能还没加载完")
    try:
        _type_field(cdp, "number", num)
        _type_field(cdp, "expiry", expd)
        _type_field(cdp, "cvc", cvc_d)
        raw = cdp.js(
            "JSON.stringify([...document.querySelectorAll('input[name]')]"
            ".map(e=>({name:e.name,value:e.value})))"
        )
        values = {v["name"]: v["value"] for v in json.loads(raw or "[]")}
    finally:
        cdp.close()
    return values


def checkout_amount(cdp):
    expr = (
        "(()=>{const ls=document.body.innerText.split(String.fromCharCode(10))"
        ".map(s=>s.trim()).filter(Boolean);"
        "const i=ls.findIndex(l=>/今日应付金额|Total due|Total today/.test(l));"
        "if(i<0)return '';"
        "const rest=ls[i].replace(/今日应付金额|Total due today|Total due|Total today/,'').trim();"
        "return rest || (ls[i+1]||'');})()"
    )
    try:
        return cdp.js(expr, await_promise=False) or ""
    except Exception:
        return ""


_CLICK_JS = r"""
(() => {
  const re = %s;
  const scope = %s;
  const els = [...scope.querySelectorAll('button,a,[role="button"]')];
  const hit = els.find(e => re.test((e.innerText || '').trim()) && !e.disabled);
  if (!hit) return '';
  hit.click();
  return (hit.innerText || '').trim().slice(0, 40);
})()
"""


def _re_literal(pattern):
    """把 '^订阅$' 或 '/^订阅$/' 统一成 JS 正则字面量。"""
    return "/" + str(pattern).strip("/") + "/"


def click_in_dialog(cdp, pattern):
    scope = "document.querySelector('[role=dialog]') || document"
    return cdp.js(_CLICK_JS % (_re_literal(pattern), scope), await_promise=False) or ""


def click_in_page(cdp, pattern):
    return cdp.js(_CLICK_JS % (_re_literal(pattern), "document"), await_promise=False) or ""


def wait_checkout(cdp, timeout=60):
    """等待 /checkout/ 页面出现，返回最终 URL。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            href = cdp.js("location.href", await_promise=False) or ""
            if CHECKOUT_HINT in href:
                return href
        except Exception:
            pass
        time.sleep(1)
    return ""


def wait_card_ready(port, timeout=60):
    cdp = find_card_frame(port, timeout=timeout)
    if cdp is None:
        return False
    cdp.close()
    return True


# --------------------------------------------------------------------------- #
# 账单地址（Stripe 第二个子 frame）
# --------------------------------------------------------------------------- #
def _find_frame(port, presence_js, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        for t in list_targets(port):
            url = t.get("url", "")
            if t.get("type") != "iframe" or CARD_FRAME_HINT not in url:
                continue
            if "elements-inner" not in url:
                continue
            try:
                cdp = CDP(t["webSocketDebuggerUrl"], timeout=20)
                if cdp.js(presence_js, await_promise=False):
                    return cdp
                cdp.close()
            except Exception:
                continue
        time.sleep(1)
    return None


def find_address_frame(port, timeout=25):
    return _find_frame(port, "document.querySelectorAll('input[name=postalCode]').length",
                       timeout=timeout)


_SET_SELECT_JS = r"""
(() => {
  const el = document.querySelector('select[name=%s]');
  if (!el) return null;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
  setter.call(el, %s);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return el.value;
})()
"""

_STATES_JS = r"""
(() => {
  const el = document.querySelector('select[name=administrativeArea]');
  if (!el) return '[]';
  return JSON.stringify([...el.options].map(o => ({ v: o.value, t: (o.textContent || '').trim() }))
    .filter(o => o.v));
})()
"""


def _pick_state(options, wanted):
    """在州/省下拉里按 值/文本 做大小写不敏感匹配。"""
    w = (wanted or "").strip()
    if not w:
        return ""
    for o in options:
        if o["v"].lower() == w.lower() or o["t"].lower() == w.lower():
            return o["v"]
    for o in options:
        if w.lower() in o["v"].lower() or w.lower() in o["t"].lower():
            return o["v"]
    return ""


def fill_address(port, name, country, postal, state, city, line1, line2=""):
    """填写 Stripe 账单地址；country 变化会重渲染，所以先填国家再填其它。"""
    cdp = find_address_frame(port, timeout=20)
    if cdp is None:
        raise RuntimeError("页面上还没有账单地址区块，请先点「展开账单地址」")
    try:
        if country:
            cdp.js(_SET_SELECT_JS % ("country", json.dumps(country.upper())), await_promise=False)
            time.sleep(1.2)

        states = json.loads(cdp.js(_STATES_JS, await_promise=False) or "[]")
        picked = _pick_state(states, state) if states else ""
        if states and not picked:
            raise RuntimeError("州/省「%s」不在下拉里，可选：%s" % (
                state, "、".join(o["t"] for o in states[:12])))
        if picked:
            cdp.js(_SET_SELECT_JS % ("administrativeArea", json.dumps(picked)), await_promise=False)
            time.sleep(0.4)

        if name:
            _type_field(cdp, "name", name)
        if postal:
            _type_field(cdp, "postalCode", postal)
        if city:
            _type_field(cdp, "locality", city)
        if line1:
            _type_field(cdp, "addressLine1", line1)
        if line2:
            _type_field(cdp, "addressLine2", line2)

        raw = cdp.js(
            "JSON.stringify([...document.querySelectorAll('input[name],select[name]')]"
            ".map(e=>({name:e.name,value:e.value})))"
        )
        values = {v["name"]: v["value"] for v in json.loads(raw or "[]")}
        return {"values": values, "state": picked, "states": [o["t"] for o in states][:50]}
    finally:
        cdp.close()


def reveal_billing(cdp, port, timeout=20):
    """点一次「订阅」触发校验，Stripe 会把账单地址区块展开（不填全不会扣款）。"""
    clicked = click_in_page(cdp, r"^订阅$|^Subscribe$")
    if not clicked:
        raise RuntimeError("没找到「订阅」按钮")
    cdp2 = find_address_frame(port, timeout=timeout)
    if cdp2 is None:
        return {"clicked": clicked, "revealed": False}
    cdp2.close()
    return {"clicked": clicked, "revealed": True}


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #
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


def nice_exp(ts):
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""
