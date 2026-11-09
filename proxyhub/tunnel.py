"""proxyhub/tunnel.py — 上游代理解析 + 建隧道 + 出口 IP 探测。

只用标准库：HTTP CONNECT（可带 Basic 认证 / TLS 到代理）和手写 SOCKS5。
中继（proxy_relay.py）和后台「测试」都走这里，保证测到的就是浏览器实际用的链路。
"""
import base64
import json
import socket
import ssl
import struct
import time
import urllib.parse


def ssl_context():
    """优先用 certifi 的根证书（macOS 自带 Python 常缺系统 CA），没有就用系统默认。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def parse_upstream(upstream):
    """拆出 scheme / host / port / auth；空串返回 None，格式不对抛 ValueError。

    兼容三种写法：
        1.2.3.4:8080                              无认证
        1.2.3.4:8080:user:pass                    代理商常见格式（Kookeey 等）
        http://user:pass@1.2.3.4:8080             URL 形式（也支持 https:// socks5://）
    """
    if not upstream:
        return None
    upstream = upstream.strip()
    if "://" not in upstream:
        parts = upstream.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, user, pwd = parts
            return {"scheme": "http", "host": host, "port": int(port), "auth": (user, pwd)}
        if len(parts) == 2 and parts[1].isdigit():
            return {"scheme": "http", "host": parts[0], "port": int(parts[1]), "auth": None}
    u = urllib.parse.urlparse(upstream if "://" in upstream else "http://" + upstream)
    scheme = (u.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError("只支持 http / https / socks5 代理")
    host = u.hostname
    if not host:
        raise ValueError("代理地址没写对，形如 1.2.3.4:8080")
    try:
        port = u.port or (1080 if scheme.startswith("socks") else 8080)
    except ValueError:
        raise ValueError("代理端口不对")
    auth = None
    if u.username:
        auth = (urllib.parse.unquote(u.username), urllib.parse.unquote(u.password or ""))
    return {"scheme": scheme, "host": host, "port": port, "auth": auth}


def _read_head(sock, limit=65536):
    """读到头部结束，返回 (头部, 剩余数据)。"""
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" not in buf:
        return buf, b""
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("上游代理提前断开")
        buf += chunk
    return buf


def _open_via_http(up, host, port, timeout):
    sock = socket.create_connection((up["host"], up["port"]), timeout=timeout)
    try:
        if up["scheme"] == "https":
            ctx = ssl_context()
            sock = ctx.wrap_socket(sock, server_hostname=up["host"])
        lines = ["CONNECT %s:%d HTTP/1.1" % (host, port), "Host: %s:%d" % (host, port)]
        if up["auth"]:
            token = base64.b64encode(("%s:%s" % up["auth"]).encode()).decode()
            lines.append("Proxy-Authorization: Basic " + token)
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head, rest = _read_head(sock)
        status = head.split(b"\r\n", 1)[0]
        if b" 200" not in status:
            raise RuntimeError("上游代理拒绝连接: %s" % status[:80].decode("latin-1"))
    except Exception:
        sock.close()
        raise
    return sock, rest


def _open_via_socks(up, host, port, timeout):
    sock = socket.create_connection((up["host"], up["port"]), timeout=timeout)
    try:
        methods = [0x00, 0x02] if up["auth"] else [0x00]
        sock.sendall(bytes([5, len(methods)] + methods))
        resp = _recv_exact(sock, 2)
        if resp[0] != 5:
            raise RuntimeError("SOCKS5 握手失败")
        if resp[1] == 0x02 and up["auth"]:
            u, p = (x.encode() for x in up["auth"])
            sock.sendall(bytes([1, len(u)]) + u + bytes([len(p)]) + p)
            if _recv_exact(sock, 2)[1] != 0:
                raise RuntimeError("SOCKS5 认证失败（账密不对或流量用完）")
        elif resp[1] != 0x00:
            raise RuntimeError("SOCKS5 不接受认证方式")
        try:
            addr = b"\x01" + socket.inet_aton(host)
        except OSError:
            addr = b"\x03" + bytes([len(host)]) + host.encode()
        sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
        head = _recv_exact(sock, 4)
        if head[1] != 0:
            raise RuntimeError("SOCKS5 连接目标失败（代码 %d）" % head[1])
        atyp = head[3]
        if atyp == 0x01:
            _recv_exact(sock, 4)
        elif atyp == 0x03:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        elif atyp == 0x04:
            _recv_exact(sock, 16)
        _recv_exact(sock, 2)
    except Exception:
        sock.close()
        raise
    return sock, b""


def open_via(up, host, port, timeout=20):
    """经上游 up（parse_upstream 的结果，None=直连）连到 host:port。

    返回 (socket, 上游多读到的数据)。
    """
    if not up:
        return socket.create_connection((host, port), timeout=timeout), b""
    if up["scheme"].startswith("socks"):
        return _open_via_socks(up, host, port, timeout)
    return _open_via_http(up, host, port, timeout)


# --------------------------------------------------------------------------- #
# 出口探测：经指定上游请求 IP 查询站，返回 ip / 国家 / 城市 / 运营商
# --------------------------------------------------------------------------- #
_PROBES = (
    ("ipinfo.io", "/json"),
    ("api.ipify.org", "/?format=json"),
)


def _https_get_json(up, host, path, timeout):
    sock, rest = open_via(up, host, 443, timeout=timeout)
    try:
        sock.settimeout(timeout)
        ctx = ssl_context()
        tls = ctx.wrap_socket(sock, server_hostname=host)
        # HTTP/1.0 + close：不用处理 chunked
        req = ("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: curl/8.4\r\n"
               "Accept: application/json\r\nConnection: close\r\n\r\n") % (path, host)
        tls.sendall(req.encode())
        buf = b""
        while True:
            chunk = tls.recv(8192)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 262144:
                break
        tls.close()
    finally:
        try:
            sock.close()
        except Exception:
            pass
    head, _, body = buf.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0]
    if b" 200" not in status:
        raise RuntimeError("%s 返回 %s" % (host, status[:40].decode("latin-1")))
    return json.loads(body.decode("utf-8", "replace"))


def probe(up, timeout=15):
    """经上游 up 探测出口；返回 {"ok", "ip", "country", "city", "org", "ms"} 或 {"ok": False, "error"}。"""
    t0 = time.time()
    last = ""
    for host, path in _PROBES:
        try:
            data = _https_get_json(up, host, path, timeout)
            return {
                "ok": True,
                "ip": data.get("ip") or "",
                "country": data.get("country") or "",
                "city": data.get("city") or "",
                "org": data.get("org") or "",
                "ms": int((time.time() - t0) * 1000),
                "at": int(time.time()),
            }
        except Exception as exc:
            last = str(exc)[:120]
    return {"ok": False, "error": last or "探测失败", "ms": int((time.time() - t0) * 1000)}
