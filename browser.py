"""browser.py — 共享的独立 Chrome 实例（管理端与客户页复用）。"""
import os
import time

import socket
import subprocess
import time

import cgpt
from store import SHOT_DIR

BROWSER_PORT = 9333
RELAY_PORT = 8898


def relay_alive(port=RELAY_PORT, timeout=0.6):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False
HERE = os.path.dirname(os.path.abspath(__file__))
BROWSER_PROFILE = os.path.join(HERE, ".chrome-gpt-profile")

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
        extra = []
        if relay_alive():
            extra = ["--proxy-server=http://127.0.0.1:%d" % RELAY_PORT,
                     "--proxy-bypass-list=127.0.0.1;localhost;::1"]
        self.proc = cgpt.launch_browser(
            self.port, self.profile, headless=False,
            log_path=os.path.join(HERE, "chrome.log"), extra_args=extra,
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

    def kill(self):
        """真正结束 Chrome 进程（close 只断 CDP）。"""
        self.close()
        try:
            subprocess.run(["pkill", "-f", self.profile], timeout=10)
        except Exception:
            pass
        time.sleep(1.5)

    def restart(self):
        self.kill()
        self.ensure()

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
        pattern = r"/立即购买|升级至|切换至|立即支付|确认支付|继续|订阅|Buy|Upgrade/"
        clicked = ""
        for _ in range(3):
            hit = cgpt.click_in_dialog(cdp, pattern) or ""
            if hit:
                clicked = clicked or hit
            time.sleep(1.5)
            href = cdp.js("location.href", await_promise=False) or ""
            if "/checkout/" in href:
                break
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
        # 结账页可能在独立标签里，优先绑定它
        tab = cgpt.find_checkout_tab(self.port, timeout=3)
        if tab:
            cdp = cgpt.CDP(tab["webSocketDebuggerUrl"])
            cdp.call("Page.enable")
        else:
            cdp = self.attach()
        pattern = r"^订阅$|^Subscribe$|^确认订阅$|^升级至|^立即支付$|^Pay$|^Buy$"
        clicked, deadline = "", time.time() + 25
        while time.time() < deadline and not clicked:
            clicked = cgpt.click_in_page(cdp, pattern) or ""
            if not clicked:
                time.sleep(1)
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


BROWSER = Browser()
