#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
admin_server.py — ChatGPT 账号管理台（本地服务 + 网页）

启动:
    python3 admin_server.py            # http://127.0.0.1:8899
    python3 admin_server.py --port 9000

能力:
    * 粘贴导出 JSON（完整 / 仅 accessToken）→ 解析、校验、入库
    * 账号列表：套餐、token 有效期、在线状态
    * 一键登录：把 sessionToken 注入独立 Chrome，打开已登录的 chatgpt.com
    * 一键 Plus / Pro：打开订阅弹窗并自动选中对应套餐（截图回传）
    * 数据落盘 data/accounts.json（0600），只监听 127.0.0.1
"""
import argparse
import hashlib
import json
import mimetypes
import os
import re
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import cgpt

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
DATA_DIR = os.path.join(HERE, "data")
SHOT_DIR = os.path.join(DATA_DIR, "shots")
STORE_PATH = os.path.join(DATA_DIR, "accounts.json")
BROWSER_PORT = 9333
BROWSER_PROFILE = os.path.join(HERE, ".chrome-gpt-profile")

os.makedirs(SHOT_DIR, exist_ok=True)

_lock = threading.RLock()
_store_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# 账号存储
# --------------------------------------------------------------------------- #
def load_store():
    with _store_lock:
        if not os.path.exists(STORE_PATH):
            return {"accounts": []}
        try:
            with open(STORE_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("accounts", [])
            return data
        except Exception:
            return {"accounts": []}


def save_store(data):
    with _store_lock:
        tmp = STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STORE_PATH)


def mask(tok):
    if not tok:
        return ""
    if len(tok) <= 24:
        return tok[:4] + "…" + tok[-4:]
    return tok[:12] + "…" + tok[-6:]


def public_account(acc, current_id=None):
    return {
        "id": acc["id"],
        "alias": acc.get("alias", ""),
        "email": acc.get("email", ""),
        "name": acc.get("name", ""),
        "plan": acc.get("plan", ""),
        "planLabel": cgpt.PLAN_LABEL.get(acc.get("plan", ""), acc.get("plan", "")),
        "accessExp": acc.get("accessExp"),
        "accessExpText": cgpt.nice_exp(acc.get("accessExp")),
        "sessionExpText": cgpt.nice_exp(acc.get("sessionExp")),
        "hasSession": bool(acc.get("sessionToken")),
        "status": acc.get("status", "unverified"),
        "lastError": acc.get("lastError", ""),
        "lastCheck": acc.get("lastCheck"),
        "createdAt": acc.get("createdAt"),
        "accessTokenMasked": mask(acc.get("accessToken", "")),
        "sessionTokenMasked": mask(acc.get("sessionToken", "")),
        "isCurrent": acc["id"] == current_id,
        "shots": acc.get("shots", {}),
    }


# --------------------------------------------------------------------------- #
# 浏览器单例
# --------------------------------------------------------------------------- #
class Browser(object):
    def __init__(self):
        self.port = BROWSER_PORT
        self.profile = BROWSER_PROFILE
        self.current_id = None      # 当前登录的账号 id
        self.current_email = ""
        self.cdp = None
        self.tab_id = None
        self.started_at = None
        self.proc = None

    # ---- 基础 ----
    def running(self):
        return cgpt.debugger_ready(self.port, 0.6) is not None

    def ensure(self):
        if self.running():
            return
        self.close()
        self.proc = cgpt.launch_browser(
            self.port, self.profile, headless=False,
            log_path=os.path.join(HERE, "chrome.log"),
        )
        self.started_at = time.time()

    def attach(self):
        """返回绑定到 chatgpt.com 标签页的 CDP；浏览器没开就先拉起来。"""
        self.ensure()
        if self.cdp is not None:
            try:
                self.cdp.js("1", await_promise=False)
                return self.cdp
            except Exception:
                self.cdp = None
        tab = cgpt.find_chatgpt_tab(self.port)
        if tab is None:
            tab = cgpt.open_tab(self.port, "https://chatgpt.com/")
            time.sleep(1.5)
        self.tab_id = tab["id"]
        self.cdp = cgpt.CDP(tab["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Network.enable")
        return self.cdp

    def close(self):
        if self.cdp is not None:
            self.cdp.close()
            self.cdp = None
        self.current_id = None
        self.current_email = ""

    # ---- 业务 ----
    def login(self, acc):
        self.ensure()
        cdp = self.attach()
        email = cgpt.login_account(cdp, acc)
        if not email:
            raise RuntimeError("登录失败：会话可能已失效，请重新导出 token")
        if acc.get("email") and email.lower() != acc["email"].lower():
            raise RuntimeError("登录到了其它账号（%s），请重试" % email)
        self.current_id = acc["id"]
        self.current_email = email
        return email

    def ensure_login(self, acc):
        self.ensure()
        if self.current_id == acc["id"] and self.current_email:
            cdp = self.attach()
            if cgpt.current_session_email(cdp):
                return cdp
        cdp = self.attach()
        if self.current_id != acc["id"] or not cgpt.current_session_email(cdp):
            self.login(acc)
            cdp = self.attach()
        return cdp

    def start_checkout(self, acc, plan):
        """打开订阅弹窗 → 点购买 → 停在 Stripe 结账页。"""
        cdp = self.ensure_login(acc)
        cgpt.open_plan_dialog(cdp, plan, os.path.join(SHOT_DIR, "%s-%s.png" % (acc["id"], plan)))
        clicked = cgpt.click_in_dialog(
            cdp, r"/立即购买|升级至|切换至|订阅|Buy|Upgrade/") or ""
        url = cgpt.wait_checkout(cdp, timeout=60)
        ready = cgpt.wait_card_ready(self.port, timeout=60) if url else False
        amount = ""
        deadline = time.time() + 20
        while time.time() < deadline and not amount:
            amount = cgpt.checkout_amount(cdp)
            if not amount:
                time.sleep(0.8)
        shot = cgpt.capture(cdp, os.path.join(SHOT_DIR, "%s-checkout.png" % acc["id"]))
        return {
            "ok": bool(url),
            "url": url,
            "clicked": clicked,
            "ready": ready,
            "amount": amount,
            "screenshotUrl": "/api/shots/%s-checkout.png?t=%d" % (acc["id"], int(time.time())),
            "screenshot": shot,
            "email": self.current_email,
        }

    def ensure_same_account(self, acc):
        """校验浏览器里当前登录的就是这个账号（重启后 current_id 可能为空）。"""
        cdp = self.attach()
        email = cgpt.current_session_email(cdp)
        if not email:
            raise RuntimeError("浏览器当前没有登录态，请先点「去结账」")
        if acc.get("email") and email.lower() != acc["email"].lower():
            raise RuntimeError("浏览器当前登录的是 %s，请先切回该账号" % email)
        self.current_id = acc["id"]
        self.current_email = email
        return cdp

    def fill_card(self, number, exp, cvc):
        vals = cgpt.fill_card(self.port, number, exp, cvc)
        num = vals.get("number", "")
        digits = "".join(ch for ch in num if ch.isdigit())
        return {
            "fields": {
                "number": ("**** **** **** " + digits[-4:]) if len(digits) >= 4 else "****",
                "expiry": vals.get("expiry", ""),
                "cvc": "***",
            },
            "count": sum(1 for k in ("number", "expiry", "cvc") if vals.get(k)),
        }

    def reveal_billing(self):
        cdp = self.attach()
        return cgpt.reveal_billing(cdp, self.port)

    def fill_address(self, fields):
        return cgpt.fill_address(
            self.port,
            name=fields.get("name", ""),
            country=fields.get("country", ""),
            postal=fields.get("postal", ""),
            state=fields.get("state", ""),
            city=fields.get("city", ""),
            line1=fields.get("line1", ""),
            line2=fields.get("line2", ""),
        )

    def submit_checkout(self):
        cdp = self.attach()
        clicked = cgpt.click_in_page(cdp, r"^订阅$|^Subscribe$|^确认订阅$|^Pay$|^Buy$") or ""
        if not clicked:
            raise RuntimeError("没找到「订阅」按钮，页面可能不在结账状态")
        deadline = time.time() + 45
        text, url = "", ""
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                url = cdp.js("location.href", await_promise=False) or ""
                text = cdp.js(
                    "(()=>{const t=document.body.innerText;return t.slice(0,600)})()",
                    await_promise=False,
                ) or ""
            except Exception:
                continue
            if "/checkout/" not in url or any(
                k in text for k in ("谢谢", "成功", "订阅已完成", "declined", "被拒绝", "出错", "错误")
            ):
                break
        return {
            "clicked": clicked,
            "url": url,
            "text": text,
            "pending": "/checkout/" in url and not any(
                k in text for k in ("谢谢", "成功", "declined", "被拒绝", "出错", "错误")),
        }

    def open_plan(self, acc, plan):
        cdp = self.ensure_login(acc)
        shot_path = os.path.join(SHOT_DIR, "%s-%s.png" % (acc["id"], plan))
        res = cgpt.open_plan_dialog(cdp, plan, shot_path)
        res["screenshotUrl"] = "/api/shots/%s-%s.png?t=%d" % (acc["id"], plan, int(time.time()))
        res["email"] = self.current_email
        return res


BROWSER = Browser()


def cgpt_cdp(browser):
    """拿到当前 chatgpt 标签页的 CDP（供截图用）。"""
    return browser.attach()


# --------------------------------------------------------------------------- #
# 业务：导入 / 校验
# --------------------------------------------------------------------------- #
def upsert_account(item, alias=""):
    at = item.get("accessToken", "").strip()
    st = (item.get("sessionToken") or "").strip()
    if not at:
        return None, "缺少 accessToken"

    info = cgpt.decode_access_token(at)
    ok, res = cgpt.check_access_token(at)
    if not ok:
        status, err = "invalid", str(res)
        info = info or {}
    else:
        status, err = "active", ""
        info.update({k: v for k, v in res.items() if v})

    uid = info.get("user_id") or hashlib.sha1(at.encode()).hexdigest()[:16]
    acc_id = re.sub(r"[^A-Za-z0-9_-]", "", uid)[:40] or hashlib.sha1(at.encode()).hexdigest()[:16]

    with _store_lock:
        store = load_store()
        cur = next((a for a in store["accounts"] if a["id"] == acc_id), None)
        now = int(time.time())
        if cur is None:
            cur = {"id": acc_id, "createdAt": now, "shots": {}}
            store["accounts"].append(cur)
        cur.update({
            "email": info.get("email", cur.get("email", "")),
            "name": info.get("name", cur.get("name", "")),
            "plan": info.get("plan", cur.get("plan", "")),
            "accessToken": at,
            "accessExp": info.get("exp"),
            "status": status,
            "lastError": err,
            "lastCheck": now,
            "alias": alias or cur.get("alias", ""),
        })
        if st:
            cur["sessionToken"] = st
        exp_epoch = cgpt.iso_to_epoch(item.get("expires"))
        if exp_epoch:
            cur["sessionExp"] = exp_epoch
        save_store(store)
    return cur, err


def recheck(acc_id):
    with _store_lock:
        store = load_store()
        acc = next((a for a in store["accounts"] if a["id"] == acc_id), None)
        if not acc:
            return None
        ok, res = cgpt.check_access_token(acc["accessToken"])
        acc["lastCheck"] = int(time.time())
        if ok:
            acc["status"] = "active"
            acc["lastError"] = ""
            if res.get("plan"):
                acc["plan"] = res["plan"]
            for k in ("email", "name"):
                if res.get(k):
                    acc[k] = res[k]
        else:
            acc["status"] = "invalid"
            acc["lastError"] = str(res)
        save_store(store)
        return acc


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CGPTAdmin/1.0"

    # ---------- 工具 ----------
    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _err(self, msg, code=400):
        self._send(code, {"error": str(msg)})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/status":
                return self.api_status()
            if path == "/api/accounts":
                return self.api_accounts()
            m = re.match(r"^/api/accounts/([^/]+)/token$", path)
            if m:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self.api_token(m.group(1), (qs.get("type") or ["access"])[0])
            m = re.match(r"^/api/accounts/([^/]+)$", path)
            if m:
                return self.api_account(m.group(1))
            if path.startswith("/api/shots/"):
                return self.static_shot(path)
            return self.static_file(path)
        except Exception as exc:
            return self._err(exc, 500)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/accounts":
                return self.api_import()
            if path == "/api/browser/start":
                with _lock:
                    BROWSER.ensure()
                return self._send(200, {"ok": True, "status": self.status_payload()})
            m = re.match(r"^/api/accounts/([^/]+)/(check|login|plan|checkout|card|subscribe|reveal|address)$", path)
            if m:
                return self.api_action(m.group(1), m.group(2))
            return self._err("未知接口", 404)
        except Exception as exc:
            return self._err(exc, 500)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        m = re.match(r"^/api/accounts/([^/]+)$", path)
        if not m:
            return self._err("未知接口", 404)
        try:
            with _store_lock:
                store = load_store()
                before = len(store["accounts"])
                store["accounts"] = [a for a in store["accounts"] if a["id"] != m.group(1)]
                save_store(store)
            if BROWSER.current_id == m.group(1):
                BROWSER.current_id = None
                BROWSER.current_email = ""
            return self._send(200, {"ok": True, "removed": before - len(store["accounts"])})
        except Exception as exc:
            return self._err(exc, 500)

    # ---------- 接口实现 ----------
    def status_payload(self):
        running = BROWSER.running()
        return {
            "browser": {
                "running": running,
                "port": BROWSER.port,
                "profile": BROWSER.profile,
                "currentId": BROWSER.current_id,
                "currentEmail": BROWSER.current_email,
            },
            "server": {"pid": os.getpid(), "time": int(time.time())},
        }

    def api_status(self):
        return self._send(200, self.status_payload())

    def api_accounts(self):
        store = load_store()
        return self._send(200, {
            "accounts": [public_account(a, BROWSER.current_id) for a in store["accounts"]],
            "status": self.status_payload(),
        })

    def api_account(self, acc_id):
        store = load_store()
        acc = next((a for a in store["accounts"] if a["id"] == acc_id), None)
        if not acc:
            return self._err("账号不存在", 404)
        return self._send(200, {"account": public_account(acc, BROWSER.current_id)})

    def api_token(self, acc_id, kind):
        store = load_store()
        acc = next((a for a in store["accounts"] if a["id"] == acc_id), None)
        if not acc:
            return self._err("账号不存在", 404)
        key = "accessToken" if kind == "access" else "sessionToken"
        return self._send(200, {"type": kind, "token": acc.get(key, "")})

    def api_import(self):
        body = self._body()
        raw = body.get("raw") or ""
        alias = (body.get("alias") or "").strip()
        items, err = cgpt.parse_session(raw)
        if err:
            return self._err(err)
        results = []
        for it in items:
            acc, perr = upsert_account(it, alias)
            if acc is None:
                results.append({"ok": False, "error": perr})
                continue
            results.append({
                "ok": acc.get("status") == "active",
                "error": perr,
                "account": public_account(acc, BROWSER.current_id),
            })
        return self._send(200, {"results": results})

    def api_action(self, acc_id, action):
        store = load_store()
        acc = next((a for a in store["accounts"] if a["id"] == acc_id), None)
        if not acc:
            return self._err("账号不存在", 404)

        if action == "check":
            acc = recheck(acc_id)
            return self._send(200, {"account": public_account(acc, BROWSER.current_id)})

        if not acc.get("sessionToken"):
            return self._err("该账号只有 accessToken，缺少 sessionToken，无法打开浏览器登录")

        if action == "login":
            with _lock:
                email = BROWSER.login(acc)
            return self._send(200, {
                "ok": True, "email": email, "status": self.status_payload(),
            })

        if action == "checkout":
            plan = (self._body().get("plan") or "").lower()
            if plan not in ("plus", "pro"):
                return self._err("plan 只能是 plus / pro")
            with _lock:
                res = BROWSER.start_checkout(acc, plan)
            with _store_lock:
                store = load_store()
                cur = next((a for a in store["accounts"] if a["id"] == acc_id), None)
                if cur is not None:
                    cur["checkout"] = {
                        "plan": plan, "url": res.get("url", ""),
                        "amount": res.get("amount", ""), "at": int(time.time()),
                    }
                    save_store(store)
            return self._send(200, res)

        if action == "card":
            body = self._body()
            with _lock:
                BROWSER.ensure_same_account(acc)
                res = BROWSER.fill_card(
                    body.get("number", ""), body.get("exp", ""), body.get("cvc", ""))
            return self._send(200, res)

        if action == "reveal":
            with _lock:
                BROWSER.ensure_same_account(acc)
                res = BROWSER.reveal_billing()
                shot = cgpt.capture(cgpt_cdp(BROWSER), os.path.join(
                    SHOT_DIR, "%s-checkout.png" % acc_id))
            res["screenshotUrl"] = "/api/shots/%s-checkout.png?t=%d" % (acc_id, int(time.time()))
            res["screenshot"] = shot
            return self._send(200, res)

        if action == "address":
            body = self._body()
            with _lock:
                BROWSER.ensure_same_account(acc)
                res = BROWSER.fill_address(body)
                shot = cgpt.capture(cgpt_cdp(BROWSER), os.path.join(
                    SHOT_DIR, "%s-checkout.png" % acc_id))
            res["screenshotUrl"] = "/api/shots/%s-checkout.png?t=%d" % (acc_id, int(time.time()))
            res["screenshot"] = shot
            return self._send(200, res)

        if action == "subscribe":
            body = self._body()
            if body.get("confirm") is not True:
                return self._err("需要显式确认（confirm=true）才会提交订阅")
            with _lock:
                BROWSER.ensure_same_account(acc)
                res = BROWSER.submit_checkout()
            return self._send(200, res)

        if action == "plan":
            plan = (self._body().get("plan") or "").lower()
            if plan not in ("plus", "pro"):
                return self._err("plan 只能是 plus / pro")
            with _lock:
                res = BROWSER.open_plan(acc, plan)
            with _store_lock:
                store = load_store()
                cur = next((a for a in store["accounts"] if a["id"] == acc_id), None)
                if cur is not None:
                    cur.setdefault("shots", {})[plan] = {
                        "url": res.get("screenshotUrl", ""),
                        "price": res.get("price", ""),
                        "at": int(time.time()),
                    }
                    save_store(store)
            res.update({"ok": True, "status": self.status_payload()})
            return self._send(200, res)

        return self._err("未知操作", 404)

    # ---------- 静态 ----------
    def static_shot(self, path):
        name = os.path.basename(path.split("?")[0])
        full = os.path.join(SHOT_DIR, name)
        if not os.path.exists(full):
            return self._err("截图不存在", 404)
        with open(full, "rb") as f:
            return self._send(200, f.read(), "image/png")

    def static_file(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._err("Not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("ChatGPT 账号管理台: http://%s:%d" % (args.host, args.port))
    print("数据目录: %s" % DATA_DIR)
    print("浏览器: 端口 %d / 配置 %s" % (BROWSER.port, BROWSER.profile))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
