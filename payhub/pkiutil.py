"""payhub/pkiutil.py — 支付签名用到的 RSA / AES 原语（只依赖已装的 cryptography）。

支付宝：RSA2 = SHA256withRSA，PKCS1v15 padding，签名/验签都对「待签串」的 UTF-8 字节。
微信 v3：请求用商户私钥 SHA256withRSA 签名；回调用微信平台证书公钥验签、APIv3 密钥 AES-256-GCM 解密。
"""
import base64

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509 import load_pem_x509_certificate
from cryptography.exceptions import InvalidSignature


def _wrap_pem(text, label):
    """支付宝后台导出的密钥常是「没有头尾、没换行」的纯 base64，补成标准 PEM。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("密钥为空")
    if "-----BEGIN" in text:
        return text.encode()
    body = "".join(text.split())
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return ("-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, lines, label)).encode()


def load_private_key(text):
    """加载 RSA 私钥：兼容 PKCS8 / PKCS1、带头尾或纯 base64。"""
    raw = (text or "").strip()
    if "-----BEGIN" in raw:
        candidates = [raw.encode()]
    else:
        candidates = [_wrap_pem(raw, "PRIVATE KEY"), _wrap_pem(raw, "RSA PRIVATE KEY")]
    last = None
    for pem in candidates:
        try:
            return serialization.load_pem_private_key(pem, password=None)
        except Exception as exc:
            last = exc
    raise ValueError("私钥解析失败：%s" % last)


def load_public_key(text):
    """加载公钥：支持裸公钥 base64 / PEM 公钥 / X.509 证书（取其公钥）。"""
    raw = (text or "").strip()
    if "-----BEGIN CERTIFICATE-----" in raw:
        return load_pem_x509_certificate(raw.encode()).public_key()
    if "-----BEGIN" in raw:
        return serialization.load_pem_public_key(raw.encode())
    return serialization.load_pem_public_key(_wrap_pem(raw, "PUBLIC KEY"))


def rsa_sign(private_key, message):
    """SHA256withRSA，返回 base64 字符串。message 为 str 或 bytes。"""
    if isinstance(message, str):
        message = message.encode("utf-8")
    sig = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def rsa_verify(public_key, message, signature_b64):
    """验签：失败返回 False，不抛异常。"""
    if isinstance(message, str):
        message = message.encode("utf-8")
    try:
        public_key.verify(base64.b64decode(signature_b64), message,
                          padding.PKCS1v15(), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def aes_gcm_decrypt(key, nonce, ciphertext_b64, associated_data=""):
    """微信 v3 回调解密：key=APIv3 密钥(32字节 str)，密文 base64（含 16 字节 tag 在尾部）。"""
    if isinstance(key, str):
        key = key.encode("utf-8")
    if isinstance(nonce, str):
        nonce = nonce.encode("utf-8")
    if isinstance(associated_data, str):
        associated_data = associated_data.encode("utf-8")
    data = base64.b64decode(ciphertext_b64)
    return AESGCM(key).decrypt(nonce, data, associated_data or None).decode("utf-8")


def cert_serial_no(cert_pem):
    """取 X.509 证书序列号（大写十六进制），微信平台证书验签时按序列号匹配。"""
    cert = load_pem_x509_certificate((cert_pem or "").strip().encode())
    return format(cert.serial_number, "X")
