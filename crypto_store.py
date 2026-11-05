"""crypto_store.py — 卡号 / CVC / token 的静态加密（Fernet / AES-128-CBC + HMAC）。

密钥来源，按优先级：
    1. 环境变量 TD_SECRET_KEY（urlsafe-base64 的 32 字节 Fernet key）
    2. data/secret.key（首次运行自动生成，权限 0600）

加密结果带 "enc::" 前缀，便于与历史明文平滑共存：
    * 读到带前缀的值 → 解密
    * 读到不带前缀的旧明文 → 原样返回（下次保存时会被加密）
这样老数据无需手动迁移，保存一次即自动转为密文。
"""
import os

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc::"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("TD_DATA_DIR") or os.path.join(HERE, "data")
KEY_PATH = os.path.join(DATA_DIR, "secret.key")

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    env = os.environ.get("TD_SECRET_KEY")
    if env:
        key = env.strip().encode()
    elif os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            key = f.read().strip()
    else:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        with open(KEY_PATH, "wb") as f:
            f.write(key)
        os.chmod(KEY_PATH, 0o600)
    _fernet = Fernet(key)
    return _fernet


def encrypt(text):
    """明文 → "enc::密文"。空值、已加密值原样返回。"""
    if text is None:
        return text
    text = str(text)
    if text == "" or text.startswith(PREFIX):
        return text
    return PREFIX + _get_fernet().encrypt(text.encode()).decode()


def decrypt(text):
    """"enc::密文" → 明文。非密文（历史明文 / 空值）原样返回；密钥不对返回空串。"""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return text
    try:
        return _get_fernet().decrypt(text[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return ""


def is_encrypted(text):
    return isinstance(text, str) and text.startswith(PREFIX)
