"""captcha.py — 登录人机校验（随机「滑块 / 字符」，SVG 渲染，零第三方依赖）。

设计要点：
    * issue() 每次随机返回一种挑战，答案只存服务端。
    * 滑块的缺口位置只体现在服务端渲染出的图里，不以数值形式下发；
      前端只能把「拖到的位置」回传，服务端比对，杜绝前端直接读到答案。
    * verify() 一次性校验：无论成功失败都作废该挑战，防重放、防对同一题暴力试。
    * 带最短作答耗时阈值，拦掉「秒答」脚本。
"""
import random
import secrets
import threading
import time

TTL = 180          # 挑战有效期（秒）
MIN_SOLVE = 0.4    # 最短作答耗时，低于视为脚本
TRACK_W = 320
TRACK_H = 170
PIECE = 46
PIECE_Y = 62
SLIDER_TOL = 7     # 滑块允许误差（px）

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 0/O/1/I 等易混淆字符
_COLORS = ["#2563eb", "#7c3aed", "#0ea5e9", "#059669", "#db2777", "#ea580c"]

_lock = threading.Lock()
_store = {}  # id -> {type, answer, issued, exp}


def _gc(now):
    for k in [k for k, v in _store.items() if v["exp"] < now]:
        _store.pop(k, None)


def _text_svg(code):
    w, h = 220, 84
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img">' % (w, h, w, h),
        '<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#eef2ff"/>'
        '<stop offset="1" stop-color="#e0e7ff"/></linearGradient></defs>',
        '<rect width="%d" height="%d" rx="12" fill="url(#bg)"/>' % (w, h),
    ]
    for _ in range(5):  # 干扰线
        x1, y1 = random.randint(0, w), random.randint(0, h)
        x2, y2 = random.randint(0, w), random.randint(0, h)
        c = random.choice(_COLORS)
        parts.append('<path d="M%d %d Q %d %d %d %d" stroke="%s" '
                     'stroke-width="1.4" fill="none" opacity="0.35"/>'
                     % (x1, y1, random.randint(0, w), random.randint(0, h), x2, y2, c))
    for i, ch in enumerate(code):
        x = 26 + i * 44 + random.randint(-4, 4)
        y = 54 + random.randint(-6, 6)
        rot = random.randint(-22, 22)
        c = random.choice(_COLORS)
        parts.append(
            '<text x="%d" y="%d" font-size="40" font-weight="800" fill="%s" '
            'font-family="Menlo,Consolas,monospace" transform="rotate(%d %d %d)">%s</text>'
            % (x, y, c, rot, x, y, ch))
    for _ in range(14):  # 噪点
        parts.append('<circle cx="%d" cy="%d" r="1.3" fill="%s" opacity="0.4"/>'
                     % (random.randint(0, w), random.randint(0, h), random.choice(_COLORS)))
    parts.append('</svg>')
    return "".join(parts)


# 滑块背景用低饱和的深色，避免花哨
_SLIDER_BG = [("#3f5466", "#1c252e"), ("#4a5a48", "#202820"), ("#5c4b42", "#2a211d"),
              ("#4b4f63", "#1f2129"), ("#3e5a5a", "#1b2626"), ("#5a5148", "#25221f")]


def _slider_bg_svg(target):
    """背景图：在 target 处画一个缺口轮廓，用户需把滑块拖进来对齐。"""
    top, bottom = random.choice(_SLIDER_BG)
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d">' % (TRACK_W, TRACK_H, TRACK_W, TRACK_H),
        '<defs><linearGradient id="sg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="%s"/>'
        '<stop offset="1" stop-color="%s"/></linearGradient></defs>' % (top, bottom),
        '<rect width="%d" height="%d" rx="14" fill="url(#sg)"/>' % (TRACK_W, TRACK_H),
    ]
    for _ in range(6):  # 装饰圆
        parts.append('<circle cx="%d" cy="%d" r="%d" fill="#ffffff" opacity="0.07"/>'
                     % (random.randint(0, TRACK_W), random.randint(0, TRACK_H),
                        random.randint(10, 34)))
    # 缺口（答案所在，前端拿不到坐标，只能视觉对齐）
    parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="9" '
                 'fill="rgba(2,6,23,0.55)" stroke="rgba(255,255,255,0.75)" '
                 'stroke-width="2" stroke-dasharray="5 4"/>'
                 % (target, PIECE_Y, PIECE, PIECE))
    parts.append('</svg>')
    return "".join(parts)


def issue():
    now = time.time()
    cid = secrets.token_urlsafe(18)
    kind = random.choice(("slider", "text"))
    with _lock:
        _gc(now)
        if kind == "text":
            code = "".join(random.choice(_ALPHABET) for _ in range(4))
            _store[cid] = {"type": "text", "answer": code.upper(),
                           "issued": now, "exp": now + TTL}
            return {"id": cid, "type": "text", "image": _text_svg(code)}
        target = random.randint(96, TRACK_W - PIECE - 8)
        _store[cid] = {"type": "slider", "answer": target,
                       "issued": now, "exp": now + TTL}
        return {"id": cid, "type": "slider", "image": _slider_bg_svg(target),
                "trackWidth": TRACK_W, "trackHeight": TRACK_H,
                "piece": PIECE, "pieceY": PIECE_Y}


def verify(cid, response):
    now = time.time()
    with _lock:
        _gc(now)
        item = _store.pop(cid or "", None)   # 一次性：取出即作废
    if not item or item["exp"] < now:
        return False
    if now - item["issued"] < MIN_SOLVE:
        return False
    response = response or {}
    if item["type"] == "text":
        ans = str(response.get("text", "")).strip().upper()
        return bool(ans) and ans == item["answer"]
    try:
        x = float(response.get("x"))
    except (TypeError, ValueError):
        return False
    return abs(x - item["answer"]) <= SLIDER_TOL
