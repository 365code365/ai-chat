"""store.py — 账号 / 卡库 / 兑换码 的本地持久化。"""
import json
import os
import re
import secrets
import threading

import cgpt
import crypto_store

# 落盘前加密、读回后解密的敏感字段
_CARD_SECRET_FIELDS = ("number", "cvc")
_ACCOUNT_SECRET_FIELDS = ("accessToken", "sessionToken")


def _transform(items, fields, fn):
    """对列表里每个 dict 的指定字段做 fn（就地）。"""
    for it in items:
        for f in fields:
            if it.get(f):
                it[f] = fn(it[f])


def _encrypted_copy(items, fields):
    """返回浅拷贝后、敏感字段已加密的列表；不改动调用方手里的明文 dict。"""
    out = [dict(it) for it in items]
    _transform(out, fields, crypto_store.encrypt)
    return out

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("TD_DATA_DIR") or os.path.join(HERE, "data")
SHOT_DIR = os.path.join(DATA_DIR, "shots")
STORE_PATH = os.path.join(DATA_DIR, "accounts.json")
CARDS_PATH = os.path.join(DATA_DIR, "cards.json")

os.makedirs(SHOT_DIR, exist_ok=True)
_store_lock = threading.RLock()

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
VALID_PLANS = ("plus", "pro")

CARD_BRANDS = [
    ("visa", "Visa", r"^4"),
    ("mastercard", "Mastercard", r"^(5[1-5]|222[1-9]|22[3-9]\d|2[3-6]\d\d|27[01]\d|2720)"),
    ("amex", "American Express", r"^3[47]"),
    ("jcb", "JCB", r"^35(2[89]|[3-8]\d)"),
    ("unionpay", "银联", r"^(62|81)"),
    ("discover", "Discover", r"^(6011|65|64[4-9])"),
    ("diners", "Diners Club", r"^(36|38|30[0-5])"),
]


# --------------------------------------------------------------------------- #
# 通用 JSON 读写
# --------------------------------------------------------------------------- #
def _read_json(path, list_key):
    """读 {list_key: [...]} 结构；文件不存在或损坏时返回空结构。"""
    with _store_lock:
        if not os.path.exists(path):
            return {list_key: []}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault(list_key, [])
            return data
        except Exception:
            return {list_key: []}


def atomic_write_json(path, data):
    """先写 .tmp（0600）再原子替换，避免写一半被读到。"""
    with _store_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def find_by_id(items, item_id):
    return next((x for x in items if x.get("id") == item_id), None)


# --------------------------------------------------------------------------- #
# 卡工具
# --------------------------------------------------------------------------- #
def gen_code(length=10):
    """生成不易混淆的兑换码，形如 AB3K-9PQX-M2。"""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    return "%s-%s-%s" % (raw[:4], raw[4:8], raw[8:])


def luhn_ok(number):
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 12:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def card_brand(number):
    for key, label, pattern in CARD_BRANDS:
        if re.match(pattern, number):
            return key, label
    return "card", "银行卡"


def mask_pan(number):
    return "•••• •••• •••• " + number[-4:] if len(number) >= 4 else "••••"


def normalize_exp(raw):
    d = re.sub(r"\D", "", raw or "")
    if len(d) != 4:
        raise ValueError("有效期格式应为 MM/YY")
    mm, yy = int(d[:2]), int(d[2:])
    if not 1 <= mm <= 12:
        raise ValueError("有效期月份不合法")
    return "%02d/%02d" % (mm, yy), mm, yy


def mask(tok):
    if not tok:
        return ""
    if len(tok) <= 24:
        return tok[:4] + "…" + tok[-4:]
    return tok[:12] + "…" + tok[-6:]


def card_account_ids(card):
    """兼容老数据：accountId 单值 → accountIds 列表。"""
    ids = card.get("accountIds")
    if isinstance(ids, list):
        return [x for x in ids if x]
    old = card.get("accountId")
    return [old] if old else []


def card_plans(card):
    """这张卡允许买的套餐，默认两个都行。"""
    plans = card.get("plans")
    if not isinstance(plans, list) or not plans:
        return list(VALID_PLANS)
    return [x for x in plans if x in VALID_PLANS] or list(VALID_PLANS)


def code_state(card):
    """兑换码状态：none（无码）/ used（已兑换）/ revoked（已作废）/ active（可用）。"""
    if not card.get("code"):
        return "none"
    if card.get("codeUsedAt"):
        return "used"
    if card.get("codeRevoked"):
        return "revoked"
    return "active"


def code_active(card):
    """兑换码是否还能用：有码、未兑换、未作废。"""
    return code_state(card) == "active"


# --------------------------------------------------------------------------- #
# 卡库
# --------------------------------------------------------------------------- #
def load_cards():
    data = _read_json(CARDS_PATH, "cards")
    _transform(data["cards"], _CARD_SECRET_FIELDS, crypto_store.decrypt)
    return data


def save_cards(data):
    out = {k: v for k, v in data.items() if k != "cards"}
    out["cards"] = _encrypted_copy(data.get("cards", []), _CARD_SECRET_FIELDS)
    atomic_write_json(CARDS_PATH, out)


def migrate_cards():
    """把单账号绑定迁移成多账号绑定。"""
    data = load_cards()
    changed = False
    for c in data["cards"]:
        if not isinstance(c.get("plans"), list) or not c.get("plans"):
            c["plans"] = list(VALID_PLANS)
            changed = True
        if not isinstance(c.get("accountIds"), list):
            c["accountIds"] = card_account_ids(c)
            changed = True
        c.pop("accountId", None)
    if changed:
        save_cards(data)


def ensure_codes():
    """给历史卡补齐兑换码。"""
    data = load_cards()
    changed = False
    for c in data["cards"]:
        if not c.get("code"):
            c["code"] = gen_code()
            changed = True
    if changed:
        save_cards(data)
    return data


def find_card_by_code(code):
    """按兑换码找卡（客户页唯一入口）。"""
    code = (code or "").strip().upper()
    if not code:
        return None
    for c in load_cards()["cards"]:
        if (c.get("code") or "").upper() == code:
            return c
    return None


def customer_card_view(card):
    """给客户看的卡信息：只暴露品牌与后四位。"""
    brand_key, brand_label = card_brand(card.get("number", ""))
    return {
        "brand": brand_label,
        "brandKey": brand_key,
        "last4": (card.get("number") or "")[-4:],
        "exp": card.get("exp", ""),
    }


def public_card(card, accounts=None):
    accounts = accounts or []
    ids = card_account_ids(card)
    linked = [{"id": a["id"], "email": a.get("email", "")}
              for a in accounts if a["id"] in ids]
    brand_key, brand_label = card_brand(card.get("number", ""))
    return {
        "id": card["id"],
        "label": card.get("label", ""),
        "brand": brand_key,
        "brandLabel": brand_label,
        "last4": (card.get("number") or "")[-4:],
        "masked": mask_pan(card.get("number", "")),
        "exp": card.get("exp") or "",
        "holder": card.get("holder", ""),
        "country": card.get("country", ""),
        "postal": card.get("postal", ""),
        "state": card.get("state", ""),
        "city": card.get("city", ""),
        "line1": card.get("line1", ""),
        "code": card.get("code", ""),
        "plans": card_plans(card),
        "codeUsedAt": card.get("codeUsedAt"),
        "codeRevoked": bool(card.get("codeRevoked")),
        "codeState": code_state(card),
        "accountIds": ids,
        "accounts": linked,
        "accountId": ids[0] if ids else None,
        "accountEmail": linked[0]["email"] if linked else "",
        "status": card.get("status", "active"),
        "createdAt": card.get("createdAt"),
        "lastUsedAt": card.get("lastUsedAt"),
    }


# --------------------------------------------------------------------------- #
# 账号
# --------------------------------------------------------------------------- #
def load_store():
    data = _read_json(STORE_PATH, "accounts")
    _transform(data["accounts"], _ACCOUNT_SECRET_FIELDS, crypto_store.decrypt)
    return data


def save_store(data):
    out = {k: v for k, v in data.items() if k != "accounts"}
    out["accounts"] = _encrypted_copy(data.get("accounts", []), _ACCOUNT_SECRET_FIELDS)
    atomic_write_json(STORE_PATH, out)


def public_account(acc, current_id=None):
    return {
        "id": acc["id"],
        "alias": acc.get("alias", ""),
        "email": acc.get("email", ""),
        "name": acc.get("name", ""),
        "plan": acc.get("plan", ""),
        "planLabel": cgpt.PLAN_LABEL.get(acc.get("plan", ""), acc.get("plan", "")),
        "accessExp": acc.get("accessExp"),
        "accessExpText": cgpt.nice_exp(acc.get("accessExp")),
        "sessionExpText": cgpt.nice_exp(acc.get("sessionExp")),
        "hasSession": bool(acc.get("sessionToken")),
        "status": acc.get("status", "unverified"),
        "lastError": acc.get("lastError", ""),
        "lastCheck": acc.get("lastCheck"),
        "createdAt": acc.get("createdAt"),
        "accessTokenMasked": mask(acc.get("accessToken", "")),
        "sessionTokenMasked": mask(acc.get("sessionToken", "")),
        "isCurrent": acc["id"] == current_id,
        "shots": acc.get("shots", {}),
    }
