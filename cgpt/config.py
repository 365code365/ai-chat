"""常量与配置"""
import base64
import datetime
import json
import os
import re
import subprocess
import time
import urllib.parse

import requests
import websocket



HERE = os.path.dirname(os.path.abspath(__file__))


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


COOKIE_BASE = "__Secure-next-auth.session-token"


CHUNK = 3800


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


# --------------------------------------------------------------------------- #
# 纯 HTTP 校验
# --------------------------------------------------------------------------- #
