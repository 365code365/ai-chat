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

import requests

import admin_auth
import cgpt
import proxy_relay
from browser import BROWSER, RELAY_PORT, Browser, relay_alive  # noqa: F401
from store import (  # noqa: F401
    DATA_DIR, SHOT_DIR, STORE_PATH, CARDS_PATH, luhn_ok, card_brand, mask_pan,
    load_cards, save_cards, public_card, normalize_exp, mask, load_store,
    save_store, public_account, gen_code, ensure_codes, migrate_cards,
    card_account_ids, card_plans, _store_lock,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")


_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# 账号存储
# --------------------------------------------------------------------------- #
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
    def _authed(self):
        return admin_auth.current_user(self.headers.get("Cookie"))

    def _guard(self, path):
        """除登录/健康检查外，/api/* 一律要求会话。"""
        if not path.startswith("/api/"):
            return True
        if path in ("/api/login", "/api/session", "/api/health"):
            return True
        return bool(self._authed())

    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._guard(path):
            return self._send(401, {"error": "未登录"})
        try:
            if path == "/api/proxy":
                return self._send(200, self.proxy_payload())
            if path == "/api/session":
                return self._send(200, {"ok": bool(self._authed()),
                                        "user": self._authed() or ""})
            if path == "/api/status":
                return self.api_status()
            if path == "/api/accounts":
                return self.api_accounts()
            if path == "/api/cards":
                return self.api_cards()
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
        if not self._guard(path):
            return self._send(401, {"error": "未登录"})
        try:
            if path == "/api/accounts":
                return self.api_import()
            if path == "/api/proxy":
                return self.api_proxy_save()
            if path == "/api/proxy/test":
                return self.api_proxy_test()
            if path == "/api/proxy/restart":
                return self.api_proxy_restart()
            if path == "/api/login":
                return self.api_login()
            if path == "/api/logout":
                return self.api_logout()
            if path == "/api/cards":
                return self.api_card_create()
            if path == "/api/import-session":
                return self.api_import_session()
            m = re.match(r"^/api/cards/([^/]+)/reveal$", path)
            if m:
                return self.api_card_reveal(m.group(1))
            m = re.match(r"^/api/cards/([^/]+)/rotate-code$", path)
            if m:
                return self.api_card_rotate(m.group(1))
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

    def do_PATCH(self):
        path = self.path.split("?")[0]
        if not self._guard(path):
            return self._send(401, {"error": "未登录"})
        m = re.match(r"^/api/cards/([^/]+)$", path)
        if not m:
            return self._err("未知接口", 404)
        try:
            return self.api_card_update(m.group(1))
        except Exception as exc:
            return self._err(exc, 500)

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if not self._guard(path):
            return self._send(401, {"error": "未登录"})
        mc = re.match(r"^/api/cards/([^/]+)$", path)
        if mc:
            try:
                return self.api_card_delete(mc.group(1))
            except Exception as exc:
                return self._err(exc, 500)
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

    # ---------- 代理 ----------
    def proxy_payload(self):
        upstream = proxy_relay.load_upstream()
        info = None
        if upstream:
            try:
                info = proxy_relay.parse_upstream(upstream)
            except Exception:
                info = None
        return {
            "upstream": upstream,
            "valid": bool(info) if upstream else True,
            "relayPort": RELAY_PORT,
            "relayAlive": relay_alive(),
            "browserUsesRelay": relay_alive(),
            "hint": "保存后点「重启浏览器」让代理生效" if upstream else "当前直连",
        }

    def api_proxy_save(self):
        body = self._body()
        upstream = (body.get("upstream") or "").strip()
        if upstream:
            try:
                proxy_relay.parse_upstream(upstream)
            except Exception as exc:
                return self._err(str(exc))
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = os.path.join(DATA_DIR, "proxy.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"upstream": upstream, "updatedAt": int(time.time())}, f,
                      ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(DATA_DIR, "proxy.json"))
        return self._send(200, self.proxy_payload())

    def api_proxy_test(self):
        if not relay_alive():
            return self._err("中继没在跑，先启动 proxy_relay.py")
        proxies = {"http": "http://127.0.0.1:%d" % RELAY_PORT,
                   "https": "http://127.0.0.1:%d" % RELAY_PORT}
        t0 = time.time()
        last = ""
        for url in ("https://api.ipify.org?format=json", "https://ifconfig.co/json"):
            try:
                r = requests.get(url, proxies=proxies, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.ok:
                    data = r.json()
                    return self._send(200, {
                        "ok": True,
                        "ip": data.get("ip") or data.get("ip_addr") or "",
                        "country": data.get("country") or data.get("country_iso") or "",
                        "ms": int((time.time() - t0) * 1000),
                    })
                last = "HTTP %d" % r.status_code
            except Exception as exc:
                last = str(exc)[:80]
        return self._send(200, {"ok": False, "error": last or "测试失败"})

    def api_proxy_restart(self):
        BROWSER.restart()
        return self._send(200, {"ok": True, "status": self.status_payload(),
                                "proxy": self.proxy_payload()})

    # ---------- 登录 ----------
    def api_login(self):
        body = self._body()
        user = body.get("user") or ""
        password = body.get("password") or ""
        if not admin_auth.verify(user, password):
            return self._send(401, {"error": "用户名或密码不对"})
        token, ttl = admin_auth.create_session(user)
        return self._send(200, {"ok": True, "user": user},
                          extra={"Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                                               % (admin_auth.COOKIE_NAME, token, ttl)})

    def api_logout(self):
        admin_auth.revoke(admin_auth.current_user(self.headers.get("Cookie")))
        return self._send(200, {"ok": True},
                          extra={"Set-Cookie": "%s=; Path=/; HttpOnly; Max-Age=0"
                                               % admin_auth.COOKIE_NAME})

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

    # ---------- 卡库接口 ----------
    def api_cards(self):
        cards = load_cards()["cards"]
        accounts = load_store()["accounts"]
        return self._send(200, {
            "cards": [public_card(c, accounts) for c in cards],
            "accounts": [{"id": a["id"], "email": a.get("email", "")} for a in accounts],
        })

    def api_card_create(self):
        body = self._body()
        number = re.sub(r"\D", "", body.get("number", ""))
        if len(number) < 12:
            return self._err("卡号位数不对")
        if not luhn_ok(number):
            return self._err("卡号没通过 Luhn 校验，检查一下是否输错")
        exp, mm, yy = normalize_exp(body.get("exp", ""))
        cvc = re.sub(r"\D", "", body.get("cvc", ""))
        if len(cvc) not in (3, 4):
            return self._err("安全码应为 3-4 位")

        account_ids = body.get("accountIds")
        if not isinstance(account_ids, list):
            account_ids = [body.get("accountId")] if body.get("accountId") else []
        account_ids = [a for a in account_ids if a]
        known = {a["id"] for a in load_store()["accounts"]}
        bad = [a for a in account_ids if a not in known]
        if bad:
            return self._err("绑定的账号不存在：%s" % bad[0])

        store = load_cards()
        card = {
            "id": hashlib.sha1((number + exp).encode()).hexdigest()[:12],
            "label": (body.get("label") or "").strip(),
            "number": number,
            "exp": exp,
            "expMonth": mm,
            "expYear": yy,
            "cvc": cvc,
            "holder": (body.get("holder") or "").strip(),
            "country": (body.get("country") or "JP").upper(),
            "postal": (body.get("postal") or "").strip(),
            "state": (body.get("state") or "").strip(),
            "city": (body.get("city") or "").strip(),
            "line1": (body.get("line1") or "").strip(),
            "accountIds": account_ids,
            "plans": [p for p in (body.get("plans") or ["plus", "pro"]) if p in ("plus", "pro")]
                     or ["plus", "pro"],
            "status": "active",
            "createdAt": int(time.time()),
            "lastUsedAt": None,
            "code": gen_code(),
            "codeUsedAt": None,
        }
        old = next((c for c in store["cards"] if c["id"] == card["id"]), None)
        if old and old.get("code"):
            card["code"] = old["code"]
            card["codeUsedAt"] = old.get("codeUsedAt")
        store["cards"] = [c for c in store["cards"] if c["id"] != card["id"]] + [card]
        save_cards(store)
        accounts = load_store()["accounts"]
        return self._send(200, {"card": public_card(card, accounts)})

    def api_card_update(self, card_id):
        body = self._body()
        store = load_cards()
        card = next((c for c in store["cards"] if c["id"] == card_id), None)
        if not card:
            return self._err("卡不存在", 404)

        if isinstance(body.get("accountIds"), list):
            known = {a["id"] for a in load_store()["accounts"]}
            ids = [a for a in body["accountIds"] if a in known]
            card["accountIds"] = ids
        if body.get("bindAccountId") or body.get("unbindAccountId"):
            ids = set(card_account_ids(card))
            if body.get("bindAccountId"):
                if body["bindAccountId"] not in {a["id"] for a in load_store()["accounts"]}:
                    return self._err("绑定的账号不存在")
                ids.add(body["bindAccountId"])
            if body.get("unbindAccountId"):
                ids.discard(body["unbindAccountId"])
            card["accountIds"] = sorted(ids)
        if "status" in body and body["status"] in ("active", "used", "disabled"):
            card["status"] = body["status"]
        if isinstance(body.get("plans"), list):
            plans = [p for p in body["plans"] if p in ("plus", "pro")]
            card["plans"] = plans or ["plus", "pro"]
        if "label" in body:
            card["label"] = (body["label"] or "").strip()
        if "holder" in body:
            card["holder"] = (body["holder"] or "").strip()
        if "postal" in body:
            card["postal"] = (body["postal"] or "").strip()
        if "state" in body:
            card["state"] = (body["state"] or "").strip()
        if "city" in body:
            card["city"] = (body["city"] or "").strip()
        if "line1" in body:
            card["line1"] = (body["line1"] or "").strip()
        save_cards(store)
        return self._send(200, {"card": public_card(card, load_store()["accounts"])})

    def api_card_delete(self, card_id):
        store = load_cards()
        before = len(store["cards"])
        store["cards"] = [c for c in store["cards"] if c["id"] != card_id]
        save_cards(store)
        return self._send(200, {"ok": True, "removed": before - len(store["cards"])})

    def api_card_rotate(self, card_id):
        store = load_cards()
        card = next((c for c in store["cards"] if c["id"] == card_id), None)
        if not card:
            return self._err("卡不存在", 404)
        card["code"] = gen_code()
        card["codeUsedAt"] = None
        save_cards(store)
        return self._send(200, {"card": public_card(card, load_store()["accounts"])})

    def api_card_reveal(self, card_id):
        store = load_cards()
        card = next((c for c in store["cards"] if c["id"] == card_id), None)
        if not card:
            return self._err("卡不存在", 404)
        return self._send(200, {"number": card["number"], "cvc": card["cvc"],
                                "exp": card["exp"]})

    def api_import_session(self):
        """给 ChatGPT 页面上的书签脚本调用：text/plain 简单请求，免 CORS 预检。"""
        body = self._body()
        raw = body.get("raw") if isinstance(body, dict) and body.get("raw") else None
        if raw is None:
            raw = json.dumps(body, ensure_ascii=False)
        items, err = cgpt.parse_session(raw)
        if err:
            return self._send(200, {"ok": False, "error": err},
                              extra={"Access-Control-Allow-Origin": "*"})
        imports = []
        for it in items:
            acc, perr = upsert_account(it)
            if acc:
                imports.append({"email": acc.get("email"), "plan": acc.get("plan")})
        return self._send(200, {
            "ok": bool(imports), "imported": imports,
            "error": "" if imports else "没能导入",
        }, extra={"Access-Control-Allow-Origin": "*"})

    def api_status(self):
        return self._send(200, self.status_payload())

    def api_accounts(self):
        store = load_store()
        cards = load_cards()["cards"]
        accounts = []
        for a in store["accounts"]:
            item = public_account(a, BROWSER.current_id)
            bound = [c for c in cards if a["id"] in card_account_ids(c)]
            item["cards"] = [{
                "id": c["id"], "masked": mask_pan(c["number"]),
                "brand": card_brand(c["number"])[1],
                "last4": c["number"][-4:], "exp": c.get("exp", ""),
            } for c in bound]
            accounts.append(item)
        return self._send(200, {
            "accounts": accounts,
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
            card = None
            if body.get("cardId"):
                cards = load_cards()["cards"]
                card = next((c for c in cards if c["id"] == body["cardId"]), None)
                if not card:
                    return self._err("卡库里没有这张卡", 404)
                if card.get("accountId") and card["accountId"] != acc_id:
                    return self._err("这张卡已绑定其它账号，先解绑再使用")
                number, exp, cvc = card["number"], card["exp"], card["cvc"]
            else:
                number, exp, cvc = (body.get("number", ""), body.get("exp", ""),
                                    body.get("cvc", ""))
            with _lock:
                BROWSER.ensure_same_account(acc)
                res = BROWSER.fill_card(number, exp, cvc)
            if card is not None:
                card["lastUsedAt"] = int(time.time())
                cards = load_cards()
                for c in cards["cards"]:
                    if c["id"] == card["id"]:
                        c["lastUsedAt"] = card["lastUsedAt"]
                save_cards(cards)
            res["cardId"] = card["id"] if card else None
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
            if body.get("cardId"):
                cards = load_cards()["cards"]
                card = next((c for c in cards if c["id"] == body["cardId"]), None)
                if not card:
                    return self._err("卡库里没有这张卡", 404)
                body = {
                    "name": card.get("holder", ""),
                    "country": card.get("country", ""),
                    "postal": card.get("postal", ""),
                    "state": card.get("state", ""),
                    "city": card.get("city", ""),
                    "line1": card.get("line1", ""),
                    "line2": "",
                }
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
            return self._send(200, f.read(), ctype,
                              extra={"Cache-Control": "no-store, must-revalidate"})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    admin_auth.ensure_credentials()
    migrate_cards()
    ensure_codes()
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
