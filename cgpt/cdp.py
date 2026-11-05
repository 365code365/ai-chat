"""CDP 与浏览器进程"""
import base64
import json
import os
import re
import subprocess
import time
import urllib.parse

import requests
import websocket

from .config import CHROME

class CDP(object):
    def __init__(self, ws_url, timeout=120):
        # 限制单帧大小（截图是最大的帧），避免 CDP 缓冲占用无上限内存。
        self.ws = websocket.create_connection(
            ws_url, max_size=48 * 1024 * 1024, suppress_origin=True, timeout=timeout
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


def _headless_ua():
    """无界面模式的默认 UA 带 HeadlessChrome 字样，换成同版本的普通 UA。"""
    try:
        out = subprocess.run([CHROME, "--version"], capture_output=True, text=True, timeout=10).stdout
        ver = re.search(r"(\d+\.\d+\.\d+\.\d+)", out).group(1)
    except Exception:
        return ""
    return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/%s Safari/537.36" % ver)


def launch_browser(port, profile, headless=False, log_path=None, extra_args=None):
    if not CHROME or not os.path.exists(CHROME):
        raise RuntimeError("没有找到浏览器（Chrome / Chromium），请先安装，或用 TD_CHROME 指定路径")
    cmd = [
        CHROME,
        "--no-sandbox",
        "--user-data-dir=%s" % profile,
        "--remote-debugging-port=%d" % port,
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1440,960",
        "--disable-dev-shm-usage",
        # —— 省内存：关掉站点隔离（每个站点一个进程是内存大户），限制渲染进程数 ——
        "--disable-features=IsolateOrigins,site-per-process,Translate,"
        "MediaRouter,OptimizationHints,InterestFeedContentSuggestions,"
        "CalculateNativeWinOcclusion",
        "--renderer-process-limit=2",
        "--disable-gpu",
        "--disable-software-rasterizer",
        # 关掉一堆用不到的后台服务，降低常驻内存和 CPU
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-component-update",
        "--disable-breakpad",
        "--disable-crash-reporter",
        "--mute-audio",
        "--metrics-recording-only",
        "--no-pings",
        # 限制 V8 老生代堆和磁盘缓存，避免单个 tab 吃太多
        "--js-flags=--max-old-space-size=256",
        "--disk-cache-size=33554432",
        "--media-cache-size=16777216",
    ]
    cmd += list(extra_args or [])
    if headless:
        cmd += ["--headless=new"]
        ua = _headless_ua()
        if ua:
            cmd.append("--user-agent=%s" % ua)
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


def list_targets(port):
    try:
        return _get("http://127.0.0.1:%d/json/list" % port, timeout=3).json()
    except Exception:
        return []


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
# 等待工具
# --------------------------------------------------------------------------- #
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
