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
import os
import re
import threading
import time
import uuid

import cgpt
from browser import POOL
from httpbase import JSONHandler, ThreadingHTTPServer
from store import (
    SHOT_DIR, VALID_PLANS, card_plans, code_active, code_state,
    customer_card_view, ensure_codes, find_card_by_code, load_cards, save_cards,
    save_job,
)

# 订阅链路的环节（key -> 中文名），兑换记录按此展示「卡在哪一步」
STEP_LABELS = {
    "queued": "排队中",
    "login": "登录 ChatGPT",
    "plan": "打开订阅页",
    "checkout": "进入付款页",
    "card": "填写支付信息",
    "address": "填写账单地址",
    "submit": "提交订阅",
    "done": "完成",
}

# 兑换码状态对应的客户提示
_CODE_BLOCK_MSG = {
    "used": "该兑换码已被使用，如需再次订阅请联系客服",
    "revoked": "该兑换码已停用，请联系客服",
    "none": "兑换码无效，请核对后重试",
}

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web_customer")

# 已结束的任务在内存里保留多久（秒），之后清理，避免 _jobs 无限增长
JOB_TTL = 6 * 3600

_jobs = {}
_job_lock = threading.Lock()
# 允许排队的最大在途任务数（池并发 + 少量排队），超过直接婉拒
MAX_PENDING = POOL.size + 3


def _add_job(job):
    now = time.time()
    with _job_lock:
        stale = [k for k, j in _jobs.items()
                 if j.get("done") and now - (j.get("finishedAt") or now) > JOB_TTL]
        for k in stale:
            _jobs.pop(k, None)
        _jobs[job["id"]] = job
    _persist(job)


# 持久化到 data/jobs.json 供管理端「兑换记录」读取。只存非敏感字段（无卡号/CVC/token）。
_PUBLIC_FIELDS = ("id", "code", "plan", "cardBrand", "cardLast4", "account",
                  "step", "stepLabel", "status", "message", "done", "ok",
                  "failedStep", "failedStepLabel", "steps", "log", "shot",
                  "createdAt", "updatedAt", "finishedAt", "durationMs")


def _persist(job):
    try:
        rec = {k: job[k] for k in _PUBLIC_FIELDS if k in job}
        save_job(rec)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 登录信息校验
#   客户在自己的浏览器里打开 chatgpt.com/api/auth/session，复制全部内容粘贴进来。
#   （不再由服务端浏览器代抓：服务端浏览器里的登录态不属于当前客户。）
# --------------------------------------------------------------------------- #
def check_session(raw):
    """返回 (item, 错误信息)。item 含 accessToken / sessionToken / expires。"""
    items, err = cgpt.parse_session(raw)
    if err or not items:
        return None, "没能识别登录信息，请打开 chatgpt.com/api/auth/session 后复制全部内容"
    item = items[0]
    if not item.get("sessionToken"):
        return None, "登录信息不完整（缺少 sessionToken），请复制该页面的全部内容"
    exp = cgpt.iso_to_epoch(item.get("expires"))
    if exp and exp < time.time():
        return None, "登录信息已过期，请刷新该页面后重新复制"
    return item, ""


# --------------------------------------------------------------------------- #
# 订阅任务
# --------------------------------------------------------------------------- #
def _set_step(job, key, status, message="", done=False, ok=None):
    now = time.time()
    steps = job.setdefault("steps", [])
    # 关掉上一个 running 的环节，记录耗时
    if steps and steps[-1]["status"] == "running":
        steps[-1]["status"] = "error" if status == "error" else "done"
        steps[-1]["durationMs"] = int((now - steps[-1]["startedAt"]) * 1000)
    if status == "running":
        steps.append({"key": key, "label": STEP_LABELS.get(key, key),
                      "status": "running", "startedAt": now, "durationMs": None})
    if status == "error":
        # 标记失败所在环节
        failed = steps[-1] if steps else None
        job["failedStep"] = (failed or {}).get("key", key)
        job["failedStepLabel"] = (failed or {}).get("label", STEP_LABELS.get(key, key))
    job["step"] = key
    job["stepLabel"] = STEP_LABELS.get(key, key)
    job["status"] = status
    job["message"] = message
    if done:
        job["done"] = True
        job["ok"] = ok
        job["finishedAt"] = int(now)
        if job.get("createdAt"):
            job["durationMs"] = int((now - job["createdAt"]) * 1000)
    job["updatedAt"] = int(now)
    _persist(job)


def _log(job, text):
    job.setdefault("log", []).append({"t": int(time.time()), "text": text})
    _persist(job)


def run_subscribe(job, card, plan, session):
    """后台跑完整订阅链路，逐步更新进度。每笔占用池里一个独立浏览器。"""
    last_cdp = None
    br = None
    try:
        # 排队等一个空闲浏览器（并发数由 POOL 限制）
        br = POOL.acquire()
        _log(job, "已分配浏览器：%s" % br.name)

        # 重新核对兑换码，挡住「同码重复下单」。
        fresh = find_card_by_code(card.get("code"))
        if not fresh or not code_active(fresh):
            raise RuntimeError("兑换码已被使用或失效，已阻止重复下单")

        items, err = cgpt.parse_session(session)
        if err or not items:
            raise RuntimeError(err or "没能解析登录信息")
        item = items[0]
        info = cgpt.decode_access_token(item.get("accessToken", ""))
        job["account"] = info.get("email", "")
        _persist(job)
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
        email = br.login(account)
        _log(job, "已登录：%s" % email)

        _set_step(job, "plan", "running", "正在打开%s订阅页…" % ("Plus" if plan == "plus" else "Pro"))
        cdp = br.attach()
        last_cdp = cdp
        shot = os.path.join(SHOT_DIR, "customer-%s.png" % job["id"])
        cgpt.open_plan_dialog(cdp, plan, shot)
        job["shot"] = "/api/shots/customer-%s.png" % job["id"]

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
        tab = cgpt.find_checkout_tab(br.port, timeout=150)
        if not tab:
            raise RuntimeError("没能进入付款页（可能已经是当前套餐，或页面加载超时）")
        url = tab.get("url", "")
        cdp = cgpt.CDP(tab["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        last_cdp = cdp
        _log(job, "付款页：%s" % url[:80])

        ready = cgpt.wait_card_ready(br.port, timeout=90)
        if not ready:
            raise RuntimeError("付款页的卡信息框没有加载出来")

        _set_step(job, "card", "running", "正在填写支付信息…")
        cgpt.fill_card(br.port, card["number"], card["exp"], card["cvc"])
        _log(job, "已填入 %s ···· %s" % (customer_card_view(card)["brand"], card["number"][-4:]))

        _set_step(job, "address", "running", "正在填写账单地址…")
        try:
            cgpt.reveal_billing(cdp, br.port)
            cgpt.fill_address(
                br.port,
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
        res = br.submit_checkout()
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
        # 失败时截一张当前页面，方便管理端排查卡在哪
        try:
            if last_cdp is not None:
                name = "customer-%s-fail.png" % job["id"]
                cgpt.capture(last_cdp, os.path.join(SHOT_DIR, name))
                job["shot"] = "/api/shots/%s" % name
        except Exception:
            pass
        _set_step(job, "error", "error", str(exc), done=True, ok=False)
        _log(job, "失败：%s" % exc)
    finally:
        if br is not None:
            # 断开本次任务的会话并归还浏览器，供下一笔复用
            try:
                br.close()
            except Exception:
                pass
            POOL.release(br)
        job.setdefault("finishedAt", int(time.time()))
        _persist(job)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(JSONHandler):
    server_version = "TokenDeskCustomer/1.0"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = self._path()
        if path == "/api/health":
            return self._send(200, {"ok": True, "time": int(time.time())})
        m = re.match(r"^/api/job/([^/]+)$", path)
        if m:
            job = _jobs.get(m.group(1))
            if not job:
                return self._err("任务不存在", 404)
            # 浅拷贝后再序列化：任务线程可能正在往 job 里加字段
            return self._send(200, dict(job))
        return self.static_file(path)

    def do_POST(self):
        path = self._path()
        try:
            if path == "/api/verify":
                return self.api_verify()
            if path == "/api/subscribe":
                return self.api_subscribe()
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
        if not code_active(card):
            # 已用 / 已作废的兑换码直接拒绝，不暴露卡信息，防盗刷
            return self._send(200, {"ok": False,
                                    "error": _CODE_BLOCK_MSG.get(code_state(card), "兑换码已失效")})
        return self._send(200, {
            "ok": True,
            "card": customer_card_view(card),
            "plans": card_plans(card),
        })

    def api_subscribe(self):
        body = self._body()
        code = (body.get("code") or "").strip().upper()
        plan = (body.get("plan") or "").lower()
        raw = body.get("session") or body.get("raw") or ""

        card = find_card_by_code(code)
        if not card:
            return self._err("兑换码无效")
        if not code_active(card):
            return self._err(_CODE_BLOCK_MSG.get(code_state(card), "兑换码已失效"))
        if plan not in VALID_PLANS:
            return self._err("请选择 Plus 或 Pro")
        if plan not in card_plans(card):
            return self._err("这个兑换码只能开通 %s" % " / ".join(
                p.capitalize() for p in card_plans(card)))
        if not str(raw).strip():
            return self._err("请先提供 ChatGPT 登录信息")
        _, serr = check_session(raw)
        if serr:
            return self._err(serr)

        pending = sum(1 for j in _jobs.values() if not j.get("done"))
        if pending >= MAX_PENDING:
            return self._err("当前开通的人较多，请稍后再试", 429)

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
        _add_job(job)

        def runner():
            run_subscribe(job, dict(card), plan, raw)

        threading.Thread(target=runner, daemon=True).start()
        return self._send(200, {"jobId": job["id"]})

    # ---------- 静态 ----------
    def static_file(self, path):
        return self._static(WEB_DIR, path)


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
