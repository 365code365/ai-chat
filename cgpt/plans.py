"""定价页与套餐弹窗"""
import json
import time
import urllib.parse

from .config import CHECKOUT_HINT
from .cdp import capture, list_targets, wait_for, wait_page_ready

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
        "const i=ls.findIndex(l=>/今日应付金额|Total due|Total today/.test(l));"
        "if(i<0)return '';"
        "const rest=ls[i].replace(/今日应付金额|Total due today|Total due|Total today/,'').trim();"
        "return rest || (ls[i+1]||'');})()"
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
