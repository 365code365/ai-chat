"""定价页与套餐弹窗"""
import json
import time
import urllib.parse

from .config import CHECKOUT_HINT
from .cdp import capture, list_targets, wait_for, wait_page_ready, wait_until

PRICING_URL = "https://chatgpt.com/pricing"

_RESOLVE_LINK_JS = r"""
(() => {
  const want = %s;
  const anchors = [...document.querySelectorAll('a[href*="highlight_plan="]')];
  const hit = anchors.find(a => {
    const href = a.getAttribute('href') || '';
    return href.indexOf('highlight_plan=' + want) >= 0 && /plus|pro/i.test(a.innerText || '');
  }) || anchors.find(a => (a.getAttribute('href') || '').indexOf('highlight_plan=' + want) >= 0);
  return hit ? hit.getAttribute('href') : '';
})()
"""

_DIALOG_JS = r"""
(() => {
  const plan = %s;
  const dlg = document.querySelector('[role="dialog"]');
  if (!dlg) return JSON.stringify({ dialog: false });
  const lines = dlg.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const lower = lines.map(s => s.toLowerCase());
  let idx = lower.indexOf(plan);
  if (idx < 0) idx = lower.findIndex(s => s === plan + ' 版' || s.indexOf(plan) === 0);
  let price = '', cta = '';
  if (idx >= 0) {
    for (let i = idx + 1; i < Math.min(lines.length, idx + 60); i++) {
      if (!price && /(JP¥|US\$|HK\$|NT\$|A\$|[$€£₱¥])\s?[\d.,]+/.test(lines[i])) price = lines[i];
      if (!cta && /升级至|升级到|切换至|切换到|已安排|当前套餐|立即购买|购买|订阅|包含|Upgrade|Continue|Buy/i.test(lines[i])) cta = lines[i];
      if (price && cta) break;
    }
  }
  return JSON.stringify({
    dialog: true,
    planFound: idx >= 0,
    price: price,
    cta: cta,
    title: (lines[0] || ''),
    snippet: lines.slice(0, 8)
  });
})()
"""


def resolve_plan_link(cdp, plan):
    """在定价页里找到「获取 Plus / 获取 Pro」按钮的真实链接。"""
    href = cdp.js(_RESOLVE_LINK_JS % json.dumps(plan))
    if href:
        return urllib.parse.urljoin("https://chatgpt.com", href)
    return ("https://chatgpt.com/?default_tab=personal&highlight_plan=%s#pricing" % plan)


def open_plan_dialog(cdp, plan, shot_path=None):
    """
    打开订阅弹窗并选中指定套餐。

    返回 {ok, url, price, cta, screenshot}
    """
    plan = (plan or "").lower()
    if plan not in ("plus", "pro"):
        raise ValueError("plan 只能是 plus 或 pro")

    # 登录时已经落在定价页就不再重复加载
    here = cdp.js("location.href", await_promise=False) or ""
    if not here.startswith(PRICING_URL):
        cdp.call("Page.navigate", url=PRICING_URL)
        wait_page_ready(cdp)
    wait_for(cdp, "!!document.querySelector('a[href*=\"highlight_plan=\"]')", timeout=20)

    url = resolve_plan_link(cdp, plan)
    cdp.call("Page.navigate", url=url)
    wait_page_ready(cdp)

    ok = wait_for(cdp, "!!document.querySelector('[role=\"dialog\"]')", timeout=30, interval=0.4)
    info = {}
    if ok:
        try:
            raw = cdp.js(_DIALOG_JS % json.dumps(plan))
            info = json.loads(raw) if raw else {}
        except Exception:
            info = {}

    shot = None
    if shot_path:
        try:
            rect = None
            if ok:
                try:
                    raw_rect = cdp.js(
                        'JSON.stringify((()=>{const d=document.querySelector(\'[role="dialog"]\');'
                        'if(!d)return null;const r=d.getBoundingClientRect();'
                        'return {x:Math.round(r.x),y:Math.round(r.y),'
                        'width:Math.round(r.width),height:Math.round(r.height)}})())'
                    )
                    rect = json.loads(raw_rect) if raw_rect else None
                except Exception:
                    rect = None
            shot = capture(cdp, shot_path, rect)
        except Exception:
            shot = None

    return {
        "ok": bool(ok),
        "url": cdp.js("location.href", await_promise=False),
        "price": info.get("price", ""),
        "cta": info.get("cta", ""),
        "planFound": bool(info.get("planFound")),
        "screenshot": shot,
    }


# --------------------------------------------------------------------------- #
# 一键进结账页：直接打开套餐弹窗 → 等「升级」按钮可点 → 点击 → 等结账页
# （不走 定价页 → 找链接 → 弹窗截图 这条老路，少一次整页加载和截图）
# --------------------------------------------------------------------------- #
PLAN_DIALOG_URL = "https://chatgpt.com/?default_tab=personal&highlight_plan=%s#pricing"
BUY_PATTERN = r"/立即购买|升级至|升级到|切换至|切换到|立即支付|确认支付|继续|订阅|Buy|Upgrade|Switch/"
CONFIRM_PATTERN = r"/立即支付|确认支付|继续前往|Continue/"

_BUY_READY_JS = r"""
(() => {
  const re = %s;
  return [...document.querySelectorAll('[role=dialog]')]
    .flatMap(d => [...d.querySelectorAll('button,a,[role=button]')])
    .some(e => re.test((e.innerText || '').trim()) && !e.disabled
               && e.getAttribute('aria-disabled') !== 'true');
})()
"""


def plan_dialog_url(plan):
    return PLAN_DIALOG_URL % plan


# 已在 chatgpt.com 页面里时，改地址 + hash 让前端路由直接弹出套餐弹窗（不整页刷新）。
# 线上实测：页内打开 1.4s，整页重新加载 7.5s。
_SPA_OPEN_JS = (
    "(()=>{history.replaceState(null,'','/?default_tab=personal&highlight_plan=%s');"
    "location.hash='pricing';return true})()"
)


def _on_chatgpt_app(href):
    return (href.startswith("https://chatgpt.com/") and "/checkout/" not in href
            and "/auth/" not in href)


def open_buy_dialog(cdp, plan, timeout=25):
    """打开指定套餐的订阅弹窗，等到购买按钮可点击。返回 True/False。"""
    plan = (plan or "").lower()
    if plan not in ("plus", "pro"):
        raise ValueError("plan 只能是 plus 或 pro")
    ready_js = _BUY_READY_JS % _re_literal(BUY_PATTERN)
    here = cdp.js("location.href", await_promise=False) or ""
    if "highlight_plan=%s" % plan in here:          # 登录时已落在弹窗页：只等按钮
        if wait_for(cdp, ready_js, timeout=timeout, interval=0.2):
            return True
    elif _on_chatgpt_app(here) and cdp.js("document.readyState", await_promise=False) == "complete":
        try:
            cdp.js(_SPA_OPEN_JS % plan, await_promise=False)
        except Exception:
            pass
        if wait_for(cdp, ready_js, timeout=6, interval=0.15):
            return True
    # 整页打开弹窗地址；不等 load 完成（静态资源拖尾），按钮可点就继续
    cdp.call("Page.navigate", url=plan_dialog_url(plan))
    if wait_for(cdp, ready_js, timeout=timeout + 15, interval=0.2):
        return True
    # 兜底：走老路径（定价页里找真实链接再打开弹窗）
    open_plan_dialog(cdp, plan)
    return wait_for(cdp, ready_js, timeout=20, interval=0.3)


def _checkout_target(port):
    for t in list_targets(port):
        if t.get("type") == "page" and CHECKOUT_HINT in t.get("url", ""):
            return t
    return None


# 点击之后页面是否「有反应」：点购买会先拉 sentinel 风控脚本，再 POST /payments/checkout。
# 只要看到这些请求，就说明这一下点击已经生效，继续等，不要再点
# （线上实测：每 2–3s 连点一次会打断流程，最慢的一次 6 连点都没进到付款页）。
# 点击时装两个观察器，把这次点击引发的请求记到 window.__tdNet：
#   PerformanceObserver：sentinel / payments/checkout 请求完成（带状态码）。
#     ChatGPT 会清空 performance 缓冲区，直接查 getEntries 会丢，观察器不受影响。
#   MutationObserver：sentinel 脚本被插入页面（点击后约 2s，比请求完成早）。
# 不改写 fetch，避免被 sentinel 风控识别。
_CLICK_MARK_JS = r"""
(() => {
  const n = window.__tdNet = { ev: [] };
  const CO = /payments\/checkout(\?|$)/;
  try {
    new PerformanceObserver(l => l.getEntries().forEach(e => {
      if (CO.test(e.name)) n.ev.push(['co', e.responseStatus || 0]);
      else if (/sentinel/.test(e.name)) n.ev.push(['sen', 0]);
    })).observe({ type: 'resource' });
  } catch (e) {}
  try {
    new MutationObserver(ms => { for (const m of ms) for (const x of m.addedNodes)
      if (x.tagName === 'SCRIPT' && /sentinel/.test(x.src || '')) n.ev.push(['sen', 0]);
    }).observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {}
  return true;
})()
"""
# 返回 nav（已跳结账页）/ limited（429 限流）/ failed（其它报错）/ progress（在走流程）/ none（没反应）
#   线上实测：约一半的首次点击会被 400「unusual activity」拒掉，隔 1s 再点即可；
#   429 checkout_creation_rate_limited 是该账号短时间内建结账会话太多，再点只会更久，直接停。
_CLICK_STATE_JS = r"""
(() => {
  if (location.href.indexOf('/checkout/') >= 0) return 'nav';
  const ev = (window.__tdNet || { ev: [] }).ev;
  const co = ev.filter(x => x[0] === 'co');
  const last = co.length ? co[co.length - 1][1] : 0;
  if (last === 429) return 'limited';
  if (last >= 400) return 'failed';
  return ev.length ? 'progress' : 'none';
})()
"""


class CheckoutRateLimited(RuntimeError):
    pass
CLICK_NO_REACTION_SECS = 8     # 点了 8s 毫无反应 → 补点
CLICK_STALL_SECS = 25          # 流程走了 25s 还没进结账页 → 补点


def _click_buy(cdp):
    try:
        cdp.js(_CLICK_MARK_JS, await_promise=False)
    except Exception:
        pass
    return click_in_dialog(cdp, CONFIRM_PATTERN) or click_in_dialog(cdp, BUY_PATTERN) or ""


def _click_state(cdp):
    try:
        return cdp.js(_CLICK_STATE_JS, await_promise=False) or "none"
    except Exception:
        return "nav"         # 页面在跳转（执行上下文被销毁）


def click_to_checkout(cdp, port, timeout=90):
    """在已打开的订阅弹窗里点购买，返回 (结账页 target, 点到的按钮文字)。

    点一次后盯着页面反应，不连点：只有「毫无反应」「创建结账会话报错」「流程卡住」时才补点，最多 3 次。
    """
    clicked = ""
    end = time.time() + timeout
    for _ in range(3):
        hit = _click_buy(cdp)
        clicked = clicked or hit
        t = time.time()
        state = "none"
        while time.time() < end:
            if _checkout_target(port):
                return find_checkout_tab(port, timeout=5), clicked
            state = _click_state(cdp)
            waited = time.time() - t
            if state == "limited":
                raise CheckoutRateLimited("该 ChatGPT 账号短时间内发起付款次数过多，被 OpenAI 限流，请稍后（约 1 小时）再试")
            if state == "nav":
                break
            if state == "failed":
                time.sleep(1)
                break
            if state == "none" and waited > CLICK_NO_REACTION_SECS:
                break
            if state == "progress" and waited > CLICK_STALL_SECS:
                break
            time.sleep(0.2)
        if state == "nav" or time.time() >= end:
            break
    return find_checkout_tab(port, timeout=max(5, end - time.time())), clicked


def goto_checkout(cdp, port, plan, timeout=90):
    """一键从任意 chatgpt.com 页面进入 plan 的结账页。返回 (target|None, 点击文字, 错误信息)。"""
    if not open_buy_dialog(cdp, plan):
        return None, "", "订阅弹窗里没有可点击的购买按钮（可能已经是该套餐，或页面没加载完）"
    tab, clicked = click_to_checkout(cdp, port, timeout=timeout)
    if not tab:
        return None, clicked, "点了「%s」但没有进入付款页" % (clicked or "购买")
    return tab, clicked, ""


def _re_literal(pattern):
    """把 '^订阅$' 或 '/^订阅$/' 统一成 JS 正则字面量。"""
    return "/" + str(pattern).strip("/") + "/"


def click_in_dialog(cdp, pattern):
    """在任意一个弹窗里点按钮（页面可能同时有两层 dialog）。"""
    scope = ("[...document.querySelectorAll('[role=dialog]')]"
             ".flatMap(d => [...d.querySelectorAll('button,a,[role=button]')])")
    return cdp.js(_CLICK_JS % (_re_literal(pattern), scope), await_promise=False) or ""


def click_in_page(cdp, pattern):
    scope = "[...document.querySelectorAll('button,a,[role=button]')]"
    return cdp.js(_CLICK_JS % (_re_literal(pattern), scope), await_promise=False) or ""


def find_checkout_tab(port, timeout=150):
    """结账页可能在新标签里打开（原标签会被关掉），直接按 target 找。"""
    end = time.time() + timeout
    while time.time() < end:
        for t in list_targets(port):
            if t.get("type") == "page" and "/checkout/" in t.get("url", ""):
                return t
        time.sleep(0.4)
    return None


def wait_checkout(cdp, timeout=60):
    """等待 /checkout/ 页面出现，返回最终 URL。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            href = cdp.js("location.href", await_promise=False) or ""
            if CHECKOUT_HINT in href:
                return href
        except Exception:
            pass
        time.sleep(0.4)
    return ""


def checkout_amount(cdp):
    expr = (
        "(()=>{const ls=document.body.innerText.split(String.fromCharCode(10))"
        ".map(s=>s.trim()).filter(Boolean);"
        # 中文「今日应付金额」/ 英文「Due today」「Total due today」（美国 IP 时是英文页）
        "const re=/今日应付金额|今日应付|Total due today|Total due|Total today|Due today/i;"
        "const i=ls.findIndex(l=>re.test(l));"
        "if(i<0)return '';"
        "const rest=ls[i].replace(re,'').replace(/^[:：\\s]+/,'').trim();"
        "const v=rest || (ls[i+1]||'');"
        "return /\\d/.test(v) ? v : '';})()"
    )
    try:
        return cdp.js(expr, await_promise=False) or ""
    except Exception:
        return ""


_CLICK_JS = r"""
(() => {
  const re = %s;
  const els = %s;
  const hit = els.find(e => re.test((e.innerText || '').trim()) && !e.disabled);
  if (!hit) return '';
  hit.click();
  return (hit.innerText || '').trim().slice(0, 40);
})()
"""
