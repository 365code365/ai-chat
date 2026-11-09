"""browser.py — 独立 Chrome 实例。

管理端手动操作用单例 BROWSER（端口 9333）；客户自动开通走浏览器池 POOL
（每个实例独立 profile + 调试端口，按需拉起，并发数由 TD_BROWSER_POOL 限制，
避免一次开太多 Chrome 把内存吃爆）。
"""
import os
import shutil
import socket
import subprocess
import threading
import time

import cgpt
import proxyhub
from store import SHOT_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
BROWSER_PORT = 9333
BROWSER_PROFILE = os.path.join(HERE, ".chrome-gpt-profile")
RELAY_PORT = proxyhub.RELAY_PORT

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
    def __init__(self, port=BROWSER_PORT, profile=BROWSER_PROFILE, name="main", spoof=True):
        self.port = port
        self.profile = profile
        self.name = name
        self.current_id = None      # 当前登录的账号 id
        self.current_email = ""
        self.cdp = None
        self.tab_id = None
        self.started_at = None
        self.proc = None
        self.slot = name                         # 代理槽位：中继上独立端口 + 独立租约
        self.relay_port = proxyhub.slot_port(name)
        self.lease = None
        # 每次启动前清空 profile 并换一份随机指纹，避免相邻两个账户设备指纹一致
        self.spoof = spoof
        self.fp = None

    # ---- 基础 ----
    def running(self):
        # 单次探测：浏览器没开时端口立即拒连，不再空等 0.6s（管理端每次刷新状态都会调）
        return cgpt.debugger_alive(self.port) is not None

    def ensure(self):
        if self.running():
            return
        self.close()
        extra = []
        port = self.relay_port if relay_alive(self.relay_port) else None
        if port is None and relay_alive(RELAY_PORT):
            port = RELAY_PORT                    # 旧版中继没有分槽位端口：退回共用端口
        if port is not None:
            # 每次启动签一份新租约：默认渠道 + 新 session ⇒ 一个新的动态住宅 IP
            self.lease = proxyhub.new_lease(self.slot, cdp_port=self.port)
            extra = ["--proxy-server=http://127.0.0.1:%d" % port,
                     "--proxy-bypass-list=127.0.0.1;localhost;::1"]
        else:
            self.lease = None
        ua, env = None, None
        if self.spoof:
            # 清空上一位客户留下的 cookie / 本地存储 / 缓存，再换一份新指纹
            self._wipe_profile()
            self.fp = cgpt.fingerprint.generate()
            extra += cgpt.fingerprint.launch_flags(self.fp)
            ua = self.fp["ua"]
            env = cgpt.fingerprint.launch_env(self.fp)
        self.proc = cgpt.launch_browser(
            self.port, self.profile, headless=cgpt.HEADLESS,
            log_path=os.path.join(HERE, "chrome-%s.log" % self.name), extra_args=extra,
            user_agent=ua, env=env,
        )
        self.started_at = time.time()

    def _wipe_profile(self):
        """删掉 profile 目录，让下一次启动是一张白纸（无 cookie / localStorage / 缓存）。"""
        try:
            shutil.rmtree(self.profile, ignore_errors=True)
        except Exception:
            pass

    def _apply_fp(self, cdp):
        if self.spoof and self.fp:
            cgpt.fingerprint.apply(cdp, self.fp)
        return cdp

    def connect(self, tab):
        """连到一个标签页（如结账页在新标签打开），带上当前指纹覆盖。"""
        cdp = cgpt.CDP(tab["webSocketDebuggerUrl"])
        cdp.call("Page.enable")
        return self._apply_fp(cdp)

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
        # 伪装指纹：在导航到 chatgpt 之前装好，首帧加载即生效
        self._apply_fp(self.cdp)
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
        proxyhub.release(self.slot)
        self.lease = None

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
        if not email and self.lease and self.lease.get("channelId"):
            # 个别住宅 IP 会被 Cloudflare 拦（页面停在挑战页）：换一个 IP 重试一次
            try:
                self.lease = proxyhub.rotate(self.slot)[0]
                email = cgpt.login_account(cdp, acc, url=landing or "https://chatgpt.com/")
            except Exception:
                email = ""
        if not email:
            raise RuntimeError("登录失败：会话可能已失效，请重新导出 token")
        if acc.get("email") and email.lower() != acc["email"].lower():
            raise RuntimeError("登录到了其它账号（%s），请重试" % email)
        self.current_id = acc["id"]
        self.current_email = email
        return email

    def ensure_login(self, acc, landing=None):
        """确保浏览器登录的是 acc；需要重新登录时直接落在 landing 页。"""
        self.ensure()
        if self.current_id == acc["id"] and self.current_email:
            cdp = self.attach()
            if cgpt.current_session_email(cdp):
                return cdp
        cdp = self.attach(blank_ok=bool(landing))
        if self.current_id != acc["id"] or not cgpt.current_session_email(cdp):
            self.login(acc, landing=landing)
            cdp = self.attach()
        return cdp

    def start_checkout(self, acc, plan):
        """一键进结账页：（需要时登录并直接落在套餐弹窗）→ 点升级 → 停在 Stripe 付款页。"""
        t0 = time.time()
        cdp = self.ensure_login(acc, landing=cgpt.plan_dialog_url(plan))
        tab, clicked, err = cgpt.goto_checkout(cdp, self.port, plan)
        shot_name = "%s-checkout.png" % acc["id"]
        shot_path = os.path.join(SHOT_DIR, shot_name)
        res = {
            "ok": False, "url": "", "clicked": clicked, "ready": False, "amount": "",
            "error": err, "email": self.current_email,
            "screenshotUrl": "/api/shots/%s?t=%d" % (shot_name, int(time.time())),
        }
        if not tab:
            res["screenshot"] = cgpt.capture(cdp, shot_path)
            res["elapsedMs"] = int((time.time() - t0) * 1000)
            return res
        # 结账页可能在原标签，也可能是新标签：直接连到它（带上同一份指纹）
        co = self.connect(tab)
        try:
            res["url"] = tab.get("url", "")
            res["ok"] = True
            res["ready"] = cgpt.wait_card_ready(self.port, timeout=60)
            deadline = time.time() + 15
            while time.time() < deadline and not res["amount"]:
                res["amount"] = cgpt.checkout_amount(co)
                if not res["amount"]:
                    time.sleep(0.4)
            res["screenshot"] = cgpt.capture(co, shot_path)
        finally:
            co.close()
        self.cdp = None            # 下次 attach 重新绑定到当前（结账）标签
        res["elapsedMs"] = int((time.time() - t0) * 1000)
        return res

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
