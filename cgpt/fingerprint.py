"""fingerprint.py — 每次开浏览器生成一份随机但自洽的设备指纹。

配合「每单换新 profile（清空 cookie / 本地存储 / 缓存）」，让相邻两次充值的
浏览器指纹不一致，避免被 Stripe / OpenAI 风控判定为「同一台设备又换账号」而拒付。

一份指纹包含：
    * User-Agent + UA Client Hints（浏览器版本、平台，二者保持一致，避免互相矛盾露馅）
    * 时区（Emulation 覆盖 + 子进程 TZ 环境变量，双保险）
    * 语言、屏幕分辨率、CPU 核数、内存
    * WebGL 厂商 / 渲染器
    * Canvas 读取噪声种子（同一会话内稳定，跨会话不同）

出口 IP 目前走美国住宅代理，所以语言固定英文、时区取美国各时区，和 IP 自洽。
"""
import json
import random

# 近几个稳定版 Chrome 大版本
_CHROME_MAJORS = ["152", "153", "154", "155", "156"]

# 平台：各字段彼此自洽（UA 片段 / navigator.platform / UA-CH / 架构 / WebGL / 常见分辨率）
_PLATFORMS = [
    {
        "name": "mac",
        "ua_os": "Macintosh; Intel Mac OS X 10_15_7",
        "nav_platform": "MacIntel",
        "ch_platform": "macOS",
        "ch_platform_version": "14.6.1",
        "arch": "arm",
        "webgl_vendor": "Google Inc. (Apple)",
        "webgl_renderers": [
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)",
        ],
        "screens": [(1440, 900), (1512, 982), (1680, 1050), (2560, 1440)],
    },
    {
        "name": "win",
        "ua_os": "Windows NT 10.0; Win64; x64",
        "nav_platform": "Win32",
        "ch_platform": "Windows",
        "ch_platform_version": "15.0.0",
        "arch": "x86",
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderers": [
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        ],
        "screens": [(1920, 1080), (1536, 864), (1366, 768), (2560, 1440)],
    },
]

# 与美国出口 IP 自洽的时区与语言
_TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver",
              "America/Los_Angeles", "America/Phoenix"]
_LANGS = ["en-US,en;q=0.9", "en-US,en;q=0.8"]
_CONCURRENCY = [4, 6, 8, 12, 16]
_MEMORY = [4, 8, 16]


def generate():
    """随机生成一份自洽的指纹。"""
    p = random.choice(_PLATFORMS)
    major = random.choice(_CHROME_MAJORS)
    ua = ("Mozilla/5.0 (%s) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/%s.0.0.0 Safari/537.36" % (p["ua_os"], major))
    w, h = random.choice(p["screens"])
    return {
        "ua": ua,
        "chrome_major": major,
        "platform": p["name"],
        "nav_platform": p["nav_platform"],
        "ch_platform": p["ch_platform"],
        "ch_platform_version": p["ch_platform_version"],
        "arch": p["arch"],
        "webgl_vendor": p["webgl_vendor"],
        "webgl_renderer": random.choice(p["webgl_renderers"]),
        "timezone": random.choice(_TIMEZONES),
        "accept_language": random.choice(_LANGS),
        "languages": ["en-US", "en"],
        "concurrency": random.choice(_CONCURRENCY),
        "memory": random.choice(_MEMORY),
        "screen_w": w,
        "screen_h": h,
        "canvas_seed": random.randint(1, 2 ** 31 - 1),
    }


def summary(fp):
    """给日志用的一行摘要（不含噪声种子）。"""
    if not fp:
        return "默认指纹"
    return "%s · Chrome%s · %dx%d · %s · %d核/%dG" % (
        fp["nav_platform"], fp["chrome_major"], fp["screen_w"], fp["screen_h"],
        fp["timezone"], fp["concurrency"], fp["memory"])


def launch_flags(fp):
    """启动参数：窗口大小、UI 语言（UA / 时区 / UA-CH 走 CDP 覆盖更可靠）。"""
    if not fp:
        return []
    return [
        "--window-size=%d,%d" % (fp["screen_w"], fp["screen_h"]),
        "--lang=en-US",
    ]


def launch_env(fp):
    """子进程额外环境变量：用 TZ 让所有渲染进程的 Date 都用这个时区（CDP 覆盖只管单标签）。"""
    if not fp:
        return {}
    return {"TZ": fp["timezone"]}


def _ua_metadata(fp):
    major = fp["chrome_major"]
    brands = [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not=A?Brand", "version": "99"},
    ]
    full = [
        {"brand": "Chromium", "version": "%s.0.0.0" % major},
        {"brand": "Google Chrome", "version": "%s.0.0.0" % major},
        {"brand": "Not=A?Brand", "version": "99.0.0.0"},
    ]
    return {
        "brands": brands,
        "fullVersionList": full,
        "fullVersion": "%s.0.0.0" % major,
        "platform": fp["ch_platform"],
        "platformVersion": fp["ch_platform_version"],
        "architecture": fp["arch"],
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


def _init_script(fp):
    """注入到每个新文档（含同进程内的 Stripe iframe）的伪装脚本。"""
    cfg = json.dumps({
        "platform": fp["nav_platform"],
        "languages": fp["languages"],
        "concurrency": fp["concurrency"],
        "memory": fp["memory"],
        "sw": fp["screen_w"],
        "sh": fp["screen_h"],
        "webglVendor": fp["webgl_vendor"],
        "webglRenderer": fp["webgl_renderer"],
        "seed": fp["canvas_seed"],
    })
    return _INIT_TEMPLATE.replace("__CFG__", cfg)


# 所有覆盖都包在 try/catch 里，任何一项失败都不影响页面正常运行
_INIT_TEMPLATE = r"""
(() => {
  const C = __CFG__;
  const def = (obj, name, val) => {
    try { Object.defineProperty(obj, name, { get: () => val, configurable: true }); } catch (e) {}
  };
  try { def(Navigator.prototype, 'webdriver', false); } catch (e) {}
  def(Navigator.prototype, 'platform', C.platform);
  def(Navigator.prototype, 'languages', Object.freeze(C.languages.slice()));
  def(Navigator.prototype, 'hardwareConcurrency', C.concurrency);
  def(Navigator.prototype, 'deviceMemory', C.memory);
  try {
    def(Screen.prototype, 'width', C.sw);
    def(Screen.prototype, 'height', C.sh);
    def(Screen.prototype, 'availWidth', C.sw);
    def(Screen.prototype, 'availHeight', C.sh - 40);
    def(Screen.prototype, 'colorDepth', 24);
    def(Screen.prototype, 'pixelDepth', 24);
  } catch (e) {}
  // WebGL 厂商 / 渲染器
  try {
    const patch = (proto) => {
      const gp = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return C.webglVendor;    // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return C.webglRenderer;  // UNMASKED_RENDERER_WEBGL
        return gp.apply(this, arguments);
      };
    };
    if (window.WebGLRenderingContext) patch(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patch(WebGL2RenderingContext.prototype);
  } catch (e) {}
  // Canvas 读取加极小噪声（种子固定 → 同一会话稳定，换会话不同）
  try {
    let s = C.seed >>> 0;
    const rnd = () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; };
    const noisify = (d) => { for (let i = 0; i < d.length; i += 4) {
      const n = (rnd() < 0.5 ? -1 : 1) * (rnd() < 0.5 ? 1 : 0);
      d[i] = Math.min(255, Math.max(0, d[i] + n));
    } };
    const C2D = CanvasRenderingContext2D.prototype;
    const gid = C2D.getImageData;
    C2D.getImageData = function () {
      const r = gid.apply(this, arguments);
      try { noisify(r.data); } catch (e) {}
      return r;
    };
    const mutate = (canvas) => {
      try {
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        const w = canvas.width, h = canvas.height;
        if (!w || !h) return;
        const img = gid.apply(ctx, [0, 0, w, h]);
        noisify(img.data);
        ctx.putImageData(img, 0, 0);
      } catch (e) {}
    };
    const proto = HTMLCanvasElement.prototype;
    const tdu = proto.toDataURL;
    proto.toDataURL = function () { mutate(this); return tdu.apply(this, arguments); };
    const tb = proto.toBlob;
    proto.toBlob = function () { mutate(this); return tb.apply(this, arguments); };
  } catch (e) {}
})();
"""


def apply(cdp, fp):
    """把指纹覆盖应用到一个标签页（CDP 连接）。已导航的页面也能即时生效（后续读取为准）。"""
    if not fp:
        return
    steps = (
        lambda: cdp.call("Emulation.setUserAgentOverride",
                         userAgent=fp["ua"],
                         acceptLanguage=fp["accept_language"],
                         platform=fp["nav_platform"],
                         userAgentMetadata=_ua_metadata(fp)),
        lambda: cdp.call("Emulation.setTimezoneOverride", timezoneId=fp["timezone"]),
        lambda: cdp.call("Page.addScriptToEvaluateOnNewDocument", source=_init_script(fp)),
        lambda: cdp.js(_init_script(fp), await_promise=False),
    )
    for step in steps:
        try:
            step()
        except Exception:
            pass
