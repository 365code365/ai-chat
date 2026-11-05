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
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse

import requests

import admin_auth
import captcha
import cgpt
import proxy_relay
from browser import BROWSER, RELAY_PORT, relay_alive
from httpbase import JSONHandler, ThreadingHTTPServer
from store import (
    DATA_DIR, SHOT_DIR, luhn_ok, card_brand, mask_pan, load_cards, save_cards,
    public_card, normalize_exp, load_store, save_store, public_account, gen_code,
    ensure_codes, migrate_cards, card_account_ids, atomic_write_json, find_by_id,
    load_jobs, VALID_PLANS, _store_lock,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PROXY_PATH = os.path.join(DATA_DIR, "proxy.json")

# 卡上可以直接 PATCH 的纯文本字段
CARD_TEXT_FIELDS = ("label", "holder", "postal", "state", "city", "line1")


def _clean_card_money(number, exp, cvc):
    """校验并规范化卡号 / 有效期 / 安全码；不合法抛 ValueError（友好信息）。"""
    num = re.sub(r"\D", "", number or "")
    if len(num) < 12:
        raise ValueError("卡号位数不对")
    if not luhn_ok(num):
        raise ValueError("卡号没通过 Luhn 校验，检查一下是否输错")
    exp_s, mm, yy = normalize_exp(exp or "")      # 本身会在格式错误时抛 ValueError
    cvc_d = re.sub(r"\D", "", cvc or "")
    if len(cvc_d) not in (3, 4):
        raise ValueError("安全码应为 3-4 位")
    return num, exp_s, mm, yy, cvc_d

_lock = threading.RLock()


def find_account(acc_id):
    return find_by_id(load_store()["accounts"], acc_id)


def shot_url(name):
    return "/api/shots/%s?t=%d" % (name, int(time.time()))


# --------------------------------------------------------------------------- #
# 业务：导入 / 校验
# --------------------------------------------------------------------------- #
def upsert_account(item, alias=""):
    at = item.get("accessToken", "").strip()
    st = (item.get("sessionToken") or "").strip()
    if not at:
        return None, "缺少 accessToken"

    info = cgpt.decode_access_token(at) or {}
    if not (info.get("email") or info.get("user_id")):
        return None, "accessToken 无法解析，请确认粘贴的是完整登录信息"

    now_ts = int(time.time())
    exp = info.get("exp")
    ok, res = cgpt.check_access_token(at)
    if ok:
        status, err = "active", ""
        info.update({k: v for k, v in res.items() if v})
    elif exp and exp < now_ts:
        status, err = "expired", "accessToken 已过期，请重新导出登录信息"
    else:
        # 在线校验失败多半是 ChatGPT 的 Cloudflare 拦了裸请求（HTTP 403），
        # token 本身未过期，按本地解析的信息正常导入，标记为「未校验」。
        status, err = "unverified", "已导入，未能在线校验（%s）" % res

    uid = info.get("user_id") or hashlib.sha1(at.encode()).hexdigest()[:16]
    acc_id = re.sub(r"[^A-Za-z0-9_-]", "", uid)[:40] or hashlib.sha1(at.encode()).hexdigest()[:16]

    with _store_lock:
        store = load_store()
        cur = find_by_id(store["accounts"], acc_id)
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
        acc = find_by_id(store["accounts"], acc_id)
        if not acc:
            return None
        now_ts = int(time.time())
        ok, res = cgpt.check_access_token(acc["accessToken"])
        acc["lastCheck"] = now_ts
        exp = (cgpt.decode_access_token(acc["accessToken"]) or {}).get("exp")
        if ok:
            acc["status"] = "active"
            acc["lastError"] = ""
            if res.get("plan"):
                acc["plan"] = res["plan"]
            for k in ("email", "name"):
                if res.get(k):
                    acc[k] = res[k]
        elif exp and exp < now_ts:
            acc["status"] = "expired"
            acc["lastError"] = "accessToken 已过期"
        else:
            acc["status"] = "unverified"
            acc["lastError"] = "未能在线校验（%s）" % res
        save_store(store)
        return acc


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(JSONHandler):
    server_version = "CGPTAdmin/1.0"

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            return
        JSONHandler.log_message(self, fmt, *args)

    # ---------- 路由 ----------
    def _authed(self):
        return admin_auth.current_user(self.headers.get("Cookie"))

    def _guard(self, path):
        """除登录/健康检查外，/api/* 一律要求会话。"""
        if not path.startswith("/api/"):
            return True
        if path in ("/api/login", "/api/session", "/api/health", "/api/captcha"):
            return True
        return bool(self._authed())

    def do_GET(self):
        path = self._path()
        if not self._guard(path):
            return self._send(401, {"error": "未登录"})
        try:
            if path == "/api/proxy":
                return self._send(200, self.proxy_payload())
            if path == "/api/captcha":
                return self._send(200, captcha.issue())
            if path == "/api/session":
                return self._send(200, {"ok": bool(self._authed()),
                                        "user": self._authed() or ""})
            if path == "/api/status":
                return self.api_status()
            if path == "/api/accounts":
                return self.api_accounts()
            if path == "/api/cards":
                return self.api_cards()
            if path == "/api/jobs":
                return self.api_jobs()
            if path == "/api/vnc":
                return self.api_vnc()
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
        path = self._path()
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
            m = re.match(r"^/api/cards/([^/]+)/revoke-code$", path)
            if m:
                return self.api_card_revoke(m.group(1))
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
        path = self._path()
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
        path = self._path()
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
                if len(store["accounts"]) != before:
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
        atomic_write_json(PROXY_PATH, {"upstream": upstream, "updatedAt": int(time.time())})
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
        wait = admin_auth.login_locked()
        if wait:
            return self._send(429, {"error": "尝试过于频繁，请 %d 秒后再试" % wait})
        if not captcha.verify(body.get("captchaId"), body.get("captcha") or {}):
            admin_auth.note_login_fail()
            return self._send(400, {"error": "人机校验未通过，请重试", "captchaFailed": True})
        user = body.get("user") or ""
        password = body.get("password") or ""
        if not admin_auth.verify(user, password):
            admin_auth.note_login_fail()
            return self._send(401, {"error": "用户名或密码不对", "captchaFailed": True})
        admin_auth.note_login_ok()
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
        try:
            number, exp, mm, yy, cvc = _clean_card_money(
                body.get("number"), body.get("exp"), body.get("cvc"))
        except ValueError as e:
            return self._err(str(e))

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
            "plans": [p for p in (body.get("plans") or VALID_PLANS) if p in VALID_PLANS]
                     or list(VALID_PLANS),
            "status": "active",
            "createdAt": int(time.time()),
            "lastUsedAt": None,
            "code": gen_code(),
            "codeUsedAt": None,
        }
        old = find_by_id(store["cards"], card["id"])
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
        card = find_by_id(store["cards"], card_id)
        if not card:
            return self._err("卡不存在", 404)

        known = {a["id"] for a in load_store()["accounts"]}
        if isinstance(body.get("accountIds"), list):
            card["accountIds"] = [a for a in body["accountIds"] if a in known]
        if body.get("bindAccountId") or body.get("unbindAccountId"):
            ids = set(card_account_ids(card))
            if body.get("bindAccountId"):
                if body["bindAccountId"] not in known:
                    return self._err("绑定的账号不存在")
                ids.add(body["bindAccountId"])
            if body.get("unbindAccountId"):
                ids.discard(body["unbindAccountId"])
            card["accountIds"] = sorted(ids)
        if "status" in body and body["status"] in ("active", "used", "disabled"):
            card["status"] = body["status"]
        if isinstance(body.get("plans"), list):
            plans = [p for p in body["plans"] if p in VALID_PLANS]
            card["plans"] = plans or list(VALID_PLANS)
        for key in CARD_TEXT_FIELDS:
            if key in body:
                card[key] = (body[key] or "").strip()
        if body.get("country"):
            card["country"] = body["country"].strip().upper()
        # 可改卡号 / 有效期 / 安全码；卡号、安全码留空＝不改（有效期必填且已回显）
        try:
            if "exp" in body and str(body.get("exp") or "").strip():
                card["exp"], card["expMonth"], card["expYear"] = normalize_exp(body["exp"])
            if re.sub(r"\D", "", body.get("number") or ""):
                num = re.sub(r"\D", "", body["number"])
                if len(num) < 12 or not luhn_ok(num):
                    raise ValueError("卡号无效，检查后重试")
                card["number"] = num
            if re.sub(r"\D", "", body.get("cvc") or ""):
                cvc_d = re.sub(r"\D", "", body["cvc"])
                if len(cvc_d) not in (3, 4):
                    raise ValueError("安全码应为 3-4 位")
                card["cvc"] = cvc_d
        except ValueError as e:
            return self._err(str(e))
        save_cards(store)
        return self._send(200, {"card": public_card(card, load_store()["accounts"])})

    def api_card_delete(self, card_id):
        store = load_cards()
        before = len(store["cards"])
        store["cards"] = [c for c in store["cards"] if c["id"] != card_id]
        if len(store["cards"]) != before:
            save_cards(store)
        return self._send(200, {"ok": True, "removed": before - len(store["cards"])})

    def api_card_rotate(self, card_id):
        store = load_cards()
        card = find_by_id(store["cards"], card_id)
        if not card:
            return self._err("卡不存在", 404)
        card["code"] = gen_code()
        card["codeUsedAt"] = None
        card["codeRevoked"] = False
        save_cards(store)
        return self._send(200, {"card": public_card(card, load_store()["accounts"])})

    def api_card_revoke(self, card_id):
        """作废 / 启用当前兑换码（不换码）。body: {revoked: bool}"""
        revoked = bool(self._body().get("revoked", True))
        store = load_cards()
        card = find_by_id(store["cards"], card_id)
        if not card:
            return self._err("卡不存在", 404)
        card["codeRevoked"] = revoked
        save_cards(store)
        return self._send(200, {"card": public_card(card, load_store()["accounts"])})

    def api_card_reveal(self, card_id):
        card = find_by_id(load_cards()["cards"], card_id)
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
        acc = find_account(acc_id)
        if not acc:
            return self._err("账号不存在", 404)
        return self._send(200, {"account": public_account(acc, BROWSER.current_id)})

    def api_token(self, acc_id, kind):
        acc = find_account(acc_id)
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
                "ok": acc.get("status") in ("active", "unverified"),
                "error": perr,
                "account": public_account(acc, BROWSER.current_id),
            })
        return self._send(200, {"results": results})

    def api_jobs(self):
        jobs = load_jobs()["jobs"]
        jobs.sort(key=lambda j: j.get("createdAt", 0), reverse=True)
        return self._send(200, {"jobs": jobs})

    def api_vnc(self):
        """生成一个临时 VNC 密码，应用到 x11vnc，返回给前端自动填充（仅线上可用）。"""
        vncpass = os.path.join(DATA_DIR, ".vncpass")
        if not shutil.which("x11vnc") or not os.path.exists(
                "/etc/systemd/system/tokendesk-x11vnc.service"):
            return self._err("本机未启用 VNC（仅线上管理端可用）", 400)
        # VNC 经典认证只取前 8 位，这里就用 8 位、去掉易混淆字符
        pw = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(8))
        try:
            subprocess.run(["x11vnc", "-storepasswd", pw, vncpass],
                           check=True, capture_output=True, timeout=10)
            os.chmod(vncpass, 0o600)
            subprocess.run(["systemctl", "restart", "tokendesk-x11vnc"],
                           check=True, capture_output=True, timeout=15)
            # 等 x11vnc 重新监听 5900，避免前端连太早
            for _ in range(30):
                with socket.socket() as s:
                    s.settimeout(0.4)
                    if s.connect_ex(("127.0.0.1", 5900)) == 0:
                        break
                time.sleep(0.2)
        except Exception as exc:
            return self._err("生成 VNC 密码失败：%s" % exc, 500)
        return self._send(200, {"password": pw, "port": 9444})

    def _update_account(self, acc_id, fn):
        """在锁内读-改-写单个账号，账号已被删除则跳过。"""
        with _store_lock:
            store = load_store()
            cur = find_by_id(store["accounts"], acc_id)
            if cur is not None:
                fn(cur)
                save_store(store)

    def _with_checkout_shot(self, res, acc_id):
        """截当前结账页，结果写回 res（reveal / address 共用）。"""
        name = "%s-checkout.png" % acc_id
        res["screenshot"] = cgpt.capture(BROWSER.attach(), os.path.join(SHOT_DIR, name))
        res["screenshotUrl"] = shot_url(name)
        return res

    def api_action(self, acc_id, action):
        acc = find_account(acc_id)
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
            self._update_account(acc_id, lambda cur: cur.update(checkout={
                "plan": plan, "url": res.get("url", ""),
                "amount": res.get("amount", ""), "at": int(time.time()),
            }))
            return self._send(200, res)

        if action == "card":
            body = self._body()
            card = None
            if body.get("cardId"):
                card = find_by_id(load_cards()["cards"], body["cardId"])
                if not card:
                    return self._err("卡库里没有这张卡", 404)
                bound = card_account_ids(card)
                if bound and acc_id not in bound:
                    return self._err("这张卡绑定的是其它账号，先解绑或改绑再使用")
                number, exp, cvc = card["number"], card["exp"], card["cvc"]
            else:
                number, exp, cvc = (body.get("number", ""), body.get("exp", ""),
                                    body.get("cvc", ""))
            with _lock:
                BROWSER.ensure_same_account(acc)
                res = BROWSER.fill_card(number, exp, cvc)
            if card is not None:
                with _store_lock:
                    cards = load_cards()
                    cur = find_by_id(cards["cards"], card["id"])
                    if cur is not None:
                        cur["lastUsedAt"] = int(time.time())
                        save_cards(cards)
            res["cardId"] = card["id"] if card else None
            return self._send(200, res)

        if action == "reveal":
            with _lock:
                BROWSER.ensure_same_account(acc)
                res = self._with_checkout_shot(BROWSER.reveal_billing(), acc_id)
            return self._send(200, res)

        if action == "address":
            body = self._body()
            if body.get("cardId"):
                card = find_by_id(load_cards()["cards"], body["cardId"])
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
                res = self._with_checkout_shot(BROWSER.fill_address(body), acc_id)
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
            self._update_account(acc_id, lambda cur: cur.setdefault("shots", {}).update({
                plan: {
                    "url": res.get("screenshotUrl", ""),
                    "price": res.get("price", ""),
                    "at": int(time.time()),
                },
            }))
            res.update({"ok": True, "status": self.status_payload()})
            return self._send(200, res)

        return self._err("未知操作", 404)

    # ---------- 静态 ----------
    def static_shot(self, path):
        name = os.path.basename(path)
        full = os.path.join(SHOT_DIR, name)
        if not os.path.exists(full):
            return self._err("截图不存在", 404)
        with open(full, "rb") as f:
            return self._send(200, f.read(), "image/png")

    def static_file(self, path):
        return self._static(WEB_DIR, path, extra={"Cache-Control": "no-store, must-revalidate"})


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
