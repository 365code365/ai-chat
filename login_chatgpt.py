#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用 ChatGPT 会话导出 JSON（accessToken / sessionToken）打开一个“已登录”的 Chrome 窗口。

用法:
    python3 login_chatgpt.py                          # 用 新建文本文档.txt 登录并打开窗口
    python3 login_chatgpt.py 另一份.json --port 9333
    python3 login_chatgpt.py --check-only             # 只校验 accessToken，不开浏览器
    python3 login_chatgpt.py --headless --port 9444 --profile /tmp/t   # 无窗口自检

说明:
    * sessionToken 长 4KB+，超过 Chrome 单 cookie 4096 字节上限，必须按 NextAuth 规则
      分块写入 __Secure-next-auth.session-token.0 / .1 ...
    * 使用工作区内的独立 user-data-dir（.chrome-gpt-profile），不碰日常 Chrome 的配置。
    * Chrome 在 DSH 沙箱里必须加 --no-sandbox，否则渲染进程起不来、调试端口不开。
"""
import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.parse

import requests
import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_BASE = "__Secure-next-auth.session-token"
CHUNK = 3800


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(
            ws_url, max_size=None, suppress_origin=True, timeout=120
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
        return r.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def debugger_ready(port, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(
                "http://127.0.0.1:%d/json/version" % port, timeout=1, proxies={}
            )
            if r.ok:
                return r.json()
        except Exception:
            pass
        time.sleep(0.3)
    return None


def cookie_expiry(sess):
    try:
        return int(
            datetime.datetime.strptime(sess["expires"], "%Y-%m-%dT%H:%M:%S.%fZ")
            .replace(tzinfo=datetime.timezone.utc)
            .timestamp()
        )
    except Exception:
        return int(time.time()) + 30 * 86400


def open_tab(port, url="about:blank"):
    r = requests.put(
        "http://127.0.0.1:%d/json/new?%s" % (port, urllib.parse.quote(url, safe=":/?&=")),
        timeout=5,
        proxies={},
    )
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?", default="新建文本文档.txt")
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--profile", default=os.path.join(HERE, ".chrome-gpt-profile"))
    ap.add_argument("--screenshot", default=os.path.join(HERE, "chatgpt_login.png"))
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--check-only", action="store_true", help="不启动浏览器，只校验 accessToken")
    args = ap.parse_args()

    path = args.session if os.path.isabs(args.session) else os.path.join(HERE, args.session)
    with open(path, encoding="utf-8") as f:
        sess = json.load(f)

    access_token = sess.get("accessToken", "")
    session_token = sess.get("sessionToken", "")
    ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
    )

    # 1) accessToken 自检
    account = None
    if access_token:
        try:
            r = requests.get(
                "https://chatgpt.com/backend-api/me",
                headers={"Authorization": "Bearer " + access_token, "User-Agent": ua},
                timeout=20,
            )
            if r.ok:
                account = r.json()
                print("[1/4] accessToken 有效: %s <%s> plan=%s" % (
                    account.get("name"), account.get("email"),
                    account.get("plan_type") or "?"))
            else:
                print("[1/4] accessToken 失效: HTTP %d" % r.status_code)
        except Exception as exc:
            print("[1/4] accessToken 校验异常: %s" % exc)

    if args.check_only:
        return 0 if account else 1
    if not session_token:
        print("导出文件里没有 sessionToken，无法做浏览器登录")
        return 2

    # 2) 启动 / 复用浏览器
    if debugger_ready(args.port, 1.5):
        proc = None
        print("[2/4] 复用端口 %d 上已运行的实例" % args.port)
    else:
        cmd = [
            CHROME,
            "--no-sandbox",
            "--user-data-dir=%s" % args.profile,
            "--remote-debugging-port=%d" % args.port,
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,960",
        ]
        if args.headless:
            cmd += ["--headless=new", "--disable-gpu"]
        cmd.append("about:blank")
        proc = subprocess.Popen(
            cmd,
            stdout=open(os.path.join(HERE, "chrome.log"), "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if not debugger_ready(args.port, 40):
            print("[2/4] Chrome 调试端口未就绪，见 chrome.log")
            return 3
        print("[2/4] Chrome 已启动 (PID %d, port %d)" % (proc.pid, args.port))

    tab = open_tab(args.port)
    cdp = CDP(tab["webSocketDebuggerUrl"])
    cdp.call("Network.enable")
    cdp.call("Page.enable")

    # 3) 分块写入 session cookie
    exp = cookie_expiry(sess)
    for n in range(8):
        cdp.call("Network.deleteCookies", name="%s.%d" % (COOKIE_BASE, n),
                 domain=".chatgpt.com")
    cdp.call("Network.deleteCookies", name=COOKIE_BASE, domain=".chatgpt.com")

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
            print("[3/4] 写入第 %d 块失败" % n)
            return 4
    print("[3/4] 已写入会话 Cookie %d 块（%d 字节）" % (len(chunks), len(session_token)))

    # 4) 打开首页并校验登录态
    cdp.call("Page.navigate", url="https://chatgpt.com/")
    if not args.headless:
        cdp.call("Page.bringToFront")

    email = None
    for _ in range(30):
        time.sleep(2)
        try:
            email = cdp.js(
                "fetch('/api/auth/session',{credentials:'include',cache:'no-store'})"
                ".then(r=>r.text()).then(t=>{try{const j=JSON.parse(t);"
                "return (j.user&&j.user.email)||''}catch(e){return ''}})"
                ".catch(()=> '')"
            )
            if email:
                break
        except Exception:
            pass

    if email:
        print("[4/4] 登录成功: %s" % email)
    else:
        print("[4/4] 未取到登录会话，页面可能仍是未登录状态")
        print("      当前标题: %s" % cdp.js("document.title", await_promise=False))

    try:
        data = cdp.call("Page.captureScreenshot", format="png")["data"]
        with open(args.screenshot, "wb") as f:
            f.write(base64.b64decode(data))
        print("      截图: %s" % args.screenshot)
    except Exception as exc:
        print("      截图失败: %s" % exc)

    cdp.close()
    print("窗口已保留%s，关闭窗口后登录态仍保存在 %s" % (
        "，PID=%d" % proc.pid if proc else "", args.profile))
    return 0 if email else 5


if __name__ == "__main__":
    sys.exit(main())
