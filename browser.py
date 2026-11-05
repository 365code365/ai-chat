"""browser.py — 独立 Chrome 实例。

管理端手动操作用单例 BROWSER（端口 9333）；客户自动开通走浏览器池 POOL
（每个实例独立 profile + 调试端口，按需拉起，并发数由 TD_BROWSER_POOL 限制，
避免一次开太多 Chrome 把内存吃爆）。
"""
import os
import socket
import subprocess
import threading
import time

import cgpt
from store import SHOT_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
BROWSER_PORT = 9333
BROWSER_PROFILE = os.path.join(HERE, ".chrome-gpt-profile")
RELAY_PORT = 8898

# 客户开通的并发浏览器数（每个约 300–500MB 内存）。默认 2，可用环境变量调。
POOL_SIZE = max(1, int(os.environ.get("TD_BROWSER_POOL", "2")))
POOL_BASE_PORT = 9340


def relay_alive(port=RELAY_PORT, timeout=0.6):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class Browser(object):
    def __init__(self, port=BROWSER_PORT, profile=BROWSER_PROFILE, name="main"):
        self.port = port
        self.profile = profile
        self.name = name
        self.current_id = None      # 当前登录的账号 id
        self.current_email = ""
        self.cdp = None
        self.tab_id = None
        self.started_at = None
        self.proc = None

    # ---- 基础 ----
    def running(self):
        # 单次探测：浏览器没开时端口立即拒连，不再空等 0.6s（管理端每次刷新状态都会调）
        return cgpt.debugger_alive(self.port) is not None

    def ensure(self):
        if self.running():
            return
        self.close()
        extra = []
        if relay_alive():
            extra = ["--proxy-server=http://127.0.0.1:%d" % RELAY_PORT,
                     "--proxy-bypass-list=127.0.0.1;localhost;::1"]
        self.proc = cgpt.launch_browser(
            self.port, self.profile, headless=cgpt.HEADLESS,
            log_path=os.path.join(HERE, "chrome-%s.log" % self.name), extra_args=extra,
        )
        self.started_at = time.time()

    def attach(self, blank_ok=False):
        """返回绑定到 chatgpt.com 标签页的 CDP；浏览器没开就先拉起来。

        blank_ok=True（登录前用）：没有 chatgpt 标签时直接复用现成的空白页，
        不先加载一遍 chatgpt.com —— 登录会写好 cookie 再导航，省掉一次整页加载。
        """
        self.ensure()
        if self.cdp is not None:
            try:
                self.cdp.js("1", await_promise=False)
                return self.cdp
            except Exception:
                self.cdp = None
        tab = cgpt.find_chatgpt_tab(self.port)
        if tab is None and blank_ok:
            tab = next((t for t in cgpt.list_pages(self.port)
                        if t.get("url", "") in ("about:blank", "")), None)
        if tab is None:
            tab = cgpt.open_tab(self.port, "about:blank" if blank_ok else "https://chatgpt.com/")
            if not blank_ok:
                time.sleep(1.5)
        self.tab_id = tab["id"]
        self.cdp = cgpt.CDP(tab["webSocketDebuggerUrl"])
        self.cdp.call("Page.enable")
        self.cdp.call("Network.enable")
        return self.cdp

    def kill(self):
        """真正结束 Chrome 进程（close 只断 CDP）。

        按调试端口精确匹配（每个实例端口唯一），避免 profile 前缀误伤：
        管理端 .chrome-gpt-profile 是池实例 .chrome-gpt-profile-c0 的子串，
        用 profile 匹配会把池里的浏览器一起杀掉。
        """
        self.close()
        try:
            subprocess.run(
                ["pkill", "-f", "remote-debugging-port=%d" % self.port],
                timeout=10,
            )
        except Exception:
            pass
        # 等进程真正退出（端口释放）再返回；顺带回收子进程，避免僵尸进程堆积
        if self.proc is not None:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None
        cgpt.wait_until(lambda: not self.running(), timeout=3, interval=0.15)

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
    def login(self, acc, landing=None):
        """写入会话并打开 landing（默认首页），返回登录到的邮箱。"""
        self.ensure()
        cdp = self.attach(blank_ok=True)
        email = cgpt.login_account(cdp, acc, url=landing or "https://chatgpt.com/")
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
        on_checkout = lambda: "/checkout/" in (cdp.js("location.href", await_promise=False) or "")
        for _ in range(3):
            hit = cgpt.click_in_dialog(cdp, pattern) or ""
            if hit:
                clicked = clicked or hit
            # 最多等 1.5s，跳到结账页就立刻继续
            if cgpt.wait_until(on_checkout, timeout=1.5):
                break
        url = cgpt.wait_checkout(cdp, timeout=60)
        ready = cgpt.wait_card_ready(self.port, timeout=60) if url else False
        amount = ""
        deadline = time.time() + 20
        while time.time() < deadline and not amount:
            amount = cgpt.checkout_amount(cdp)
            if not amount:
                time.sleep(0.4)
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
                time.sleep(0.4)
        if not clicked:
            raise RuntimeError("没找到「订阅」按钮，页面可能不在结账状态")
        deadline = time.time() + 45
        text, url = "", ""
        while time.time() < deadline:
            time.sleep(0.75)
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


# 管理端手动操作的单例浏览器
BROWSER = Browser()


# --------------------------------------------------------------------------- #
# 客户开通的浏览器池：按需拉起，最多同时 POOL_SIZE 个
# --------------------------------------------------------------------------- #
class BrowserPool(object):
    def __init__(self, size=POOL_SIZE):
        self.size = size
        self.browsers = [
            Browser(port=POOL_BASE_PORT + i,
                    profile=os.path.join(HERE, ".chrome-gpt-profile-c%d" % i),
                    name="c%d" % i)
            for i in range(size)
        ]
        self._free = list(self.browsers)
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(size)

    def acquire(self, timeout=180):
        """取一个空闲浏览器；池满则阻塞排队，超时抛错。"""
        if not self._sem.acquire(timeout=timeout):
            raise RuntimeError("当前开通的人较多，请稍后再试")
        with self._lock:
            return self._free.pop()

    def release(self, br):
        with self._lock:
            if br not in self._free:
                self._free.append(br)
        self._sem.release()

    def inflight(self):
        """正在使用中的浏览器数。"""
        with self._lock:
            return self.size - len(self._free)


POOL = BrowserPool()
