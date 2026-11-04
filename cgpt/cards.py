"""Stripe 卡信息与账单地址"""
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

from .config import *  # noqa
from .cdp import CDP, list_targets  # noqa
from .plans import click_in_page  # noqa

CHECKOUT_HINT = "/checkout/"


CARD_FRAME_HINT = "js.stripe.com"


def find_card_frame(port, timeout=45):
    """找到装着卡号/有效期/CVC 的 Stripe 子 frame，返回已连接的 CDP。"""
    end = time.time() + timeout
    while time.time() < end:
        for t in list_targets(port):
            url = t.get("url", "")
            if t.get("type") != "iframe" or CARD_FRAME_HINT not in url:
                continue
            if "elements-inner" not in url:
                continue
            try:
                cdp = CDP(t["webSocketDebuggerUrl"], timeout=20)
                if cdp.js("document.querySelectorAll('input[name=number]').length",
                          await_promise=False):
                    return cdp
                cdp.close()
            except Exception:
                continue
        time.sleep(1)
    return None


_CARD_FIELD_JS = r"""
(() => {
  const el = document.querySelector('input[name=%s]');
  if (!el) return false;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(el, '');
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.focus();
  return true;
})()
"""


def _find_frame(port, presence_js, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        for t in list_targets(port):
            url = t.get("url", "")
            if t.get("type") != "iframe" or CARD_FRAME_HINT not in url:
                continue
            if "elements-inner" not in url:
                continue
            try:
                cdp = CDP(t["webSocketDebuggerUrl"], timeout=20)
                if cdp.js(presence_js, await_promise=False):
                    return cdp
                cdp.close()
            except Exception:
                continue
        time.sleep(1)
    return None


def _type_field(cdp, name, text):
    ok = cdp.js(_CARD_FIELD_JS % json.dumps(name), await_promise=False)
    if not ok:
        raise RuntimeError("结账页找不到字段: %s" % name)
    time.sleep(0.12)
    cdp.call("Input.insertText", text=text)
    time.sleep(0.2)


def digits(s):
    return re.sub(r"\D", "", s or "")


def fill_card(port, number, exp, cvc, timeout=45):
    """把卡信息填进 Stripe 表单，返回回读到的值。"""
    num = digits(number)
    expd = digits(exp)
    cvc_d = digits(cvc)
    if len(num) < 12:
        raise ValueError("卡号位数不对")
    if len(expd) not in (4, 6):
        raise ValueError("有效期格式应为 MM/YY")
    if len(cvc_d) not in (3, 4):
        raise ValueError("安全码应为 3-4 位")

    cdp = find_card_frame(port, timeout=timeout)
    if cdp is None:
        raise RuntimeError("没有找到 Stripe 卡信息输入框，结账页可能还没加载完")
    try:
        _type_field(cdp, "number", num)
        _type_field(cdp, "expiry", expd)
        _type_field(cdp, "cvc", cvc_d)
        raw = cdp.js(
            "JSON.stringify([...document.querySelectorAll('input[name]')]"
            ".map(e=>({name:e.name,value:e.value})))"
        )
        values = {v["name"]: v["value"] for v in json.loads(raw or "[]")}
    finally:
        cdp.close()
    return values


def _pick_state(options, wanted):
    """在州/省下拉里按 值/文本 做大小写不敏感匹配。"""
    w = (wanted or "").strip()
    if not w:
        return ""
    for o in options:
        if o["v"].lower() == w.lower() or o["t"].lower() == w.lower():
            return o["v"]
    for o in options:
        if w.lower() in o["v"].lower() or w.lower() in o["t"].lower():
            return o["v"]
    return ""


def fill_address(port, name, country, postal, state, city, line1, line2=""):
    """填写 Stripe 账单地址；country 变化会重渲染，所以先填国家再填其它。"""
    cdp = find_address_frame(port, timeout=20)
    if cdp is None:
        raise RuntimeError("页面上还没有账单地址区块，请先点「展开账单地址」")
    try:
        if country:
            cdp.js(_SET_SELECT_JS % ("country", json.dumps(country.upper())), await_promise=False)
            time.sleep(1.2)

        states = json.loads(cdp.js(_STATES_JS, await_promise=False) or "[]")
        picked = _pick_state(states, state) if states else ""
        if states and not picked:
            raise RuntimeError("州/省「%s」不在下拉里，可选：%s" % (
                state, "、".join(o["t"] for o in states[:12])))
        if picked:
            cdp.js(_SET_SELECT_JS % ("administrativeArea", json.dumps(picked)), await_promise=False)
            time.sleep(0.4)

        if name:
            _type_field(cdp, "name", name)
        if postal:
            _type_field(cdp, "postalCode", postal)
        if city:
            _type_field(cdp, "locality", city)
        if line1:
            _type_field(cdp, "addressLine1", line1)
        if line2:
            _type_field(cdp, "addressLine2", line2)

        raw = cdp.js(
            "JSON.stringify([...document.querySelectorAll('input[name],select[name]')]"
            ".map(e=>({name:e.name,value:e.value})))"
        )
        values = {v["name"]: v["value"] for v in json.loads(raw or "[]")}
        return {"values": values, "state": picked, "states": [o["t"] for o in states][:50]}
    finally:
        cdp.close()


def find_address_frame(port, timeout=25):
    return _find_frame(port, "document.querySelectorAll('input[name=postalCode]').length",
                       timeout=timeout)


_SET_SELECT_JS = r"""
(() => {
  const el = document.querySelector('select[name=%s]');
  if (!el) return null;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
  setter.call(el, %s);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return el.value;
})()
"""

_STATES_JS = r"""
(() => {
  const el = document.querySelector('select[name=administrativeArea]');
  if (!el) return '[]';
  return JSON.stringify([...el.options].map(o => ({ v: o.value, t: (o.textContent || '').trim() }))
    .filter(o => o.v));
})()
"""


def reveal_billing(cdp, port, timeout=20):
    """展开账单地址：地址框已存在则直接返回，否则点一次提交按钮触发校验。"""
    probe = find_address_frame(port, timeout=3)
    if probe is not None:
        probe.close()
        return {"clicked": "", "revealed": True}
    clicked = click_in_page(cdp, r"^订阅$|^Subscribe$|^升级至|^立即支付$")
    if not clicked:
        raise RuntimeError("没找到「订阅」按钮")
    cdp2 = find_address_frame(port, timeout=timeout)
    if cdp2 is None:
        return {"clicked": clicked, "revealed": False}
    cdp2.close()
    return {"clicked": clicked, "revealed": True}


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #


def wait_card_ready(port, timeout=60):
    cdp = find_card_frame(port, timeout=timeout)
    if cdp is None:
        return False
    cdp.close()
    return True


# --------------------------------------------------------------------------- #
# 账单地址（Stripe 第二个子 frame）
# --------------------------------------------------------------------------- #
