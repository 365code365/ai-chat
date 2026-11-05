"""常量与配置"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_chrome():
    """按平台找浏览器：TD_CHROME 环境变量 > macOS Chrome > Linux 上的 chrome / chromium。"""
    env = os.environ.get("TD_CHROME")
    if env:
        return env
    mac = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if os.path.exists(mac):
        return mac
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return ""


CHROME = _find_chrome()

# Linux 服务器没有桌面时用无界面模式；TD_HEADLESS=1/0 可强制指定
_default_headless = "1" if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") else "0"
HEADLESS = os.environ.get("TD_HEADLESS", _default_headless) == "1"

COOKIE_BASE = "__Secure-next-auth.session-token"

CHUNK = 3800

# 结账页 URL 特征（plans.wait_checkout 等使用）
CHECKOUT_HINT = "/checkout/"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

PLAN_LABEL = {
    "free": "免费版",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "prolite": "Pro Lite",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "edu": "Edu",
}
