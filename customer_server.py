#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
customer_server.py — 客户兑换页（与后台完全隔离）

启动:
    python3 customer_server.py --port 8900

流程:
    兑换码 → 匹配卡（客户只看到 •••• 后四位）→ 选 Plus/Pro
    → 粘贴 ChatGPT 登录态（或书签抓取）→ 一键订阅（后台自动登录、填卡、填地址、提交）

安全边界:
    * 只监听 127.0.0.1，不挂载任何后台接口
    * 客户看不到完整卡号、卡库、账号列表
    * 兑换码为唯一入口，提交后标记已使用
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
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import cgpt
from browser import BROWSER
from store import (  # noqa: F401
    SHOT_DIR, card_plans, customer_card_view, ensure_codes, find_card_by_code,
    load_cards, load_store, save_cards,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web_customer")

_jobs = {}
_job_lock = threading.Lock()
_run_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 抓取当前浏览器里的 ChatGPT 登录态
# --------------------------------------------------------------------------- #
SESSION_URL = "https://chatgpt.com/api/auth/session"


def grab_session(timeout=25):
    """在受控 Chrome 里打开 /api/auth/session，把 JSON 读回来。"""
    BROWSER.ensure()
    tab = cgpt.open_tab(BROWSER.port, SESSION_URL)
    text = ""
    cdp = None
    try:
        time.sleep(1.0)
        cdp = cgpt.CDP(tab["webSocketDebuggerUrl"], timeout=30)
        cdp.call("Page.enable")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = cdp.js("document.body ? document.body.innerText : ''",
                             await_promise=False) or ""
            except Exception:
                raw = ""
            raw = raw.strip()
            if raw.startswith("{"):
                text = raw
                break
            time.sleep(0.6)
    finally:
        if cdp is not None:
            cdp.close()
        cgpt.close_tab(BROWSER.port, tab["id"])

    if not text:
        raise RuntimeError("没拿到登录信息，请确认这个浏览器里已经登录 chatgpt.com")
    try:
        data = json.loads(text)
    except Exception:
        raise RuntimeError("返回内容不是 JSON，可能被 Cloudflare 拦了，重试一次")
    if not data.get("accessToken"):
        raise RuntimeError("当前浏览器未登录 ChatGPT")
    return json.dumps(data, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 订阅任务
# --------------------------------------------------------------------------- #
def _set_step(job, key, status, message="", done=False, ok=None):
    job["step"] = key
    job["status"] = status
    job["message"] = message
    if done:
        job["done"] = True
        job["ok"] = ok
    job["updatedAt"] = int(time.time())


def _log(job, text):
    job.setdefault("log", []).append({"t": int(time.time()), "text": text})


def run_subscribe(job, card, plan, session):
    """后台跑完整订阅链路，逐步更新进度。"""
    try:
        items, err = cgpt.parse_session(session)
        if err or not items:
            raise RuntimeError(err or "没能解析登录信息")
        item = items[0]
        info = cgpt.decode_access_token(item.get("accessToken", ""))
        account = {
            "id": "cust-" + hashlib.sha1(item["accessToken"].encode()).hexdigest()[:12],
            "email": info.get("email", ""),
            "name": info.get("name", ""),
            "accessToken": item["accessToken"],
            "sessionToken": item.get("sessionToken", ""),
            "sessionExp": cgpt.iso_to_epoch(item.get("expires")),
        }
        if not account["sessionToken"]:
            raise RuntimeError("只有 accessToken，缺少 sessionToken，无法登录浏览器")

        _set_step(job, "login", "running", "正在登录 ChatGPT…")
        _log(job, "导入登录态：%s" % (account["email"] or "未知账号"))
        email = BROWSER.login(account)
        _log(job, "已登录：%s" % email)

        _set_step(job, "plan", "running", "正在打开%s订阅页…" % ("Plus" if plan == "plus" else "Pro"))
        cdp = BROWSER.attach()
        shot = os.path.join(SHOT_DIR, "customer-%s.png" % job["id"])
        cgpt.open_plan_dialog(cdp, plan, shot)

        _set_step(job, "checkout", "running", "正在进入付款页…")
        confirm = r"/立即支付|确认支付|继续前往|Continue/"
        purchase = r"/立即购买|升级至|切换至|订阅|Buy|Upgrade/"
        for _ in range(4):
            hit = cgpt.click_in_dialog(cdp, confirm) or cgpt.click_in_dialog(cdp, purchase) or ""
            if hit:
                _log(job, "点击：%s" % hit)
            time.sleep(1.5)
            if "/checkout/" in (cdp.js("location.href", await_promise=False) or ""):
                break
        tab = cgpt.find_checkout_tab(BROWSER.port, timeout=150)
        if not tab:
            raise RuntimeError("没能进入付款页（可能已经是当前套餐，或页面加载超时）")
        url = tab.get("url", "")
        cdp = cgpt.CDP(tab["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        _log(job, "付款页：%s" % url[:80])

        ready = cgpt.wait_card_ready(BROWSER.port, timeout=90)
        if not ready:
            raise RuntimeError("付款页的卡信息框没有加载出来")

        _set_step(job, "card", "running", "正在填写支付信息…")
        cgpt.fill_card(BROWSER.port, card["number"], card["exp"], card["cvc"])
        _log(job, "已填入 %s ···· %s" % (customer_card_view(card)["brand"], card["number"][-4:]))

        _set_step(job, "address", "running", "正在填写账单地址…")
        try:
            cgpt.reveal_billing(cdp, BROWSER.port)
            cgpt.fill_address(
                BROWSER.port,
                name=card.get("holder", ""),
                country=card.get("country", "JP"),
                postal=card.get("postal", ""),
                state=card.get("state", ""),
                city=card.get("city", ""),
                line1=card.get("line1", ""),
            )
            _log(job, "账单地址已填：%s %s" % (card.get("postal", ""), card.get("city", "")))
        except Exception as exc:
            # 部分结账页不要求账单地址，跳过继续
            _log(job, "跳过账单地址：%s" % str(exc)[:60])

        _set_step(job, "submit", "running", "正在提交订阅…")
        res = BROWSER.submit_checkout()
        text = (res.get("text") or "").strip()
        _log(job, text.split("\n")[0][:120] if text else "已提交")

        pending = bool(res.get("pending"))
        card["codeUsedAt"] = int(time.time())
        card["lastUsedAt"] = int(time.time())
        data = load_cards()
        for c in data["cards"]:
            if c["id"] == card["id"]:
                c["codeUsedAt"] = card["codeUsedAt"]
                c["lastUsedAt"] = card["lastUsedAt"]
        save_cards(data)

        _set_step(job, "done", "done",
                  "已提交，请到 ChatGPT 查看订阅状态" if pending else "订阅结果已返回",
                  done=True, ok=True)
    except Exception as exc:
        _set_step(job, "error", "error", str(exc), done=True, ok=False)
        _log(job, "失败：%s" % exc)
    finally:
        job["finishedAt"] = int(time.time())


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TokenDeskCustomer/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
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
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self._send(200, {"ok": True, "time": int(time.time())})
        m = re.match(r"^/api/job/([^/]+)$", path)
        if m:
            job = _jobs.get(m.group(1))
            if not job:
                return self._err("任务不存在", 404)
            return self._send(200, job)
        return self.static_file(path)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/verify":
                return self.api_verify()
            if path == "/api/subscribe":
                return self.api_subscribe()
            if path == "/api/grab-session":
                return self.api_grab_session()
            return self._err("未知接口", 404)
        except Exception as exc:
            return self._err(exc, 500)

    # ---------- 接口 ----------
    def api_verify(self):
        body = self._body()
        code = (body.get("code") or "").strip().upper()
        card = find_card_by_code(code)
        if not card:
            return self._send(200, {"ok": False, "error": "兑换码无效，请核对后重试"})
        view = customer_card_view(card)
        used = bool(card.get("codeUsedAt"))
        allowed = card_plans(card)
        return self._send(200, {
            "ok": True,
            "card": view,
            "used": used,
            "plans": allowed,
        })

    def api_grab_session(self):
        if _run_lock.locked():
            return self._err("正在处理订阅，稍后再试", 429)
        try:
            session = grab_session()
        except Exception as exc:
            return self._send(200, {"ok": False, "error": str(exc)})
        return self._send(200, {"ok": True, "session": session})

    def api_subscribe(self):
        body = self._body()
        code = (body.get("code") or "").strip().upper()
        plan = (body.get("plan") or "").lower()
        raw = body.get("session") or body.get("raw") or ""

        card = find_card_by_code(code)
        if not card:
            return self._err("兑换码无效")
        if plan not in ("plus", "pro"):
            return self._err("请选择 Plus 或 Pro")
        if plan not in card_plans(card):
            return self._err("这个兑换码只能开通 %s" % " / ".join(
                p.capitalize() for p in card_plans(card)))
        if not str(raw).strip():
            return self._err("请先提供 ChatGPT 登录信息")

        if _run_lock.locked():
            return self._err("前面还有订单在处理，请稍等几十秒再试", 429)

        job = {
            "id": uuid.uuid4().hex[:12],
            "code": code,
            "plan": plan,
            "step": "queued",
            "status": "queued",
            "message": "排队中…",
            "done": False,
            "ok": None,
            "log": [],
            "createdAt": int(time.time()),
        }
        _jobs[job["id"]] = job

        def runner():
            with _run_lock:
                run_subscribe(job, dict(card), plan, raw)

        threading.Thread(target=runner, daemon=True).start()
        return self._send(200, {"jobId": job["id"]})

    # ---------- 静态 ----------
    def static_file(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            return self._err("Not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    ensure_codes()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("客户兑换页: http://%s:%d" % (args.host, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
