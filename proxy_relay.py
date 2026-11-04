#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proxy_relay.py — 本地代理中继

Chrome 启动时固定指向 http://127.0.0.1:8898，真正的上游由 data/proxy.json 决定：
    ""                                    直连
    "http://user:pass@1.2.3.4:8080"       HTTP 代理
    "https://user:pass@1.2.3.4:8080"      HTTPS 代理（TLS 到代理）
    "socks5://user:pass@1.2.3.4:1080"     SOCKS5

好处：改代理只改配置文件，浏览器不用重启；对 HTTPS 只做 CONNECT 隧道，不碰证书。

启动:
    python3 proxy_relay.py --port 8898
"""
import argparse
import base64
import json
import os
import select
import socket
import socketserver
import struct
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "data", "proxy.json")
DEFAULT_PORT = 8898

_cache = {"mtime": 0, "upstream": ""}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_upstream():
    """每次请求读一次（带 mtime 缓存），改配置立即生效。"""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return ""
    if mtime != _cache["mtime"]:
        _cache["mtime"] = mtime
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                _cache["upstream"] = (json.load(f).get("upstream") or "").strip()
        except Exception:
            _cache["upstream"] = ""
    return _cache["upstream"]


def parse_upstream(upstream):
    """拆出 scheme / host / port / auth。

    兼容三种写法：
        1.2.3.4:8080                              无认证
        1.2.3.4:8080:user:pass                    代理商常见格式（Kookeey 等）
        http://user:pass@1.2.3.4:8080             URL 形式（也支持 socks5://）
    """
    if not upstream:
        return None
    upstream = upstream.strip()
    if "://" not in upstream:
        parts = upstream.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            return {"scheme": "http", "host": host, "port": int(port),
                    "auth": (user, pwd)}
        if len(parts) == 2 and parts[1].isdigit():
            return {"scheme": "http", "host": parts[0], "port": int(parts[1]),
                    "auth": None}
    u = urllib.parse.urlparse(upstream if "://" in upstream else "http://" + upstream)
    scheme = (u.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError("只支持 http / https / socks5 代理")
    host = u.hostname
    if not host:
        raise ValueError("代理地址没写对，形如 1.2.3.4:8080")
    port = u.port or (1080 if scheme.startswith("socks") else 8080)
    auth = None
    if u.username:
        auth = (urllib.parse.unquote(u.username), urllib.parse.unquote(u.password or ""))
    return {"scheme": scheme, "host": host, "port": port, "auth": auth}


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def _recv_until(sock, marker=b"\r\n\r\n", limit=65536):
    buf = b""
    while marker not in buf and len(buf) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _read_head(sock, limit=65536):
    """读到头部结束，返回 (头部, 剩余数据)。"""
    buf = b""
    sock.settimeout(15)
    while b"\r\n\r\n" not in buf and len(buf) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    if b"\r\n\r\n" not in buf:
        return buf, b""
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _open_via_http_proxy(up, host, port, tls=False):
    sock = socket.create_connection((up["host"], up["port"]), timeout=20)
    if tls:
        import ssl
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=up["host"])
    lines = ["CONNECT %s:%d HTTP/1.1" % (host, port), "Host: %s:%d" % (host, port)]
    if up["auth"]:
        token = base64.b64encode(("%s:%s" % up["auth"]).encode()).decode()
        lines.append("Proxy-Authorization: Basic " + token)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    head, rest = _read_head(sock)
    status = head.split(b"\r\n", 1)[0]
    if b" 200" not in status:
        sock.close()
        raise RuntimeError("上游代理拒绝连接: %r" % status[:80])
    sock.settimeout(60)
    return sock, rest


def _open_via_socks(up, host, port):
    sock = socket.create_connection((up["host"], up["port"]), timeout=20)
    methods = [0x00]
    if up["auth"]:
        methods.append(0x02)
    sock.sendall(bytes([5, len(methods)] + methods))
    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 5:
        sock.close()
        raise RuntimeError("SOCKS5 握手失败")
    if resp[1] == 0x02 and up["auth"]:
        u, p = up["auth"]
        req = bytes([1, len(u)]) + u.encode() + bytes([len(p)]) + p.encode()
        sock.sendall(req)
        if sock.recv(2)[1] != 0:
            sock.close()
            raise RuntimeError("SOCKS5 认证失败")
    elif resp[1] != 0x00:
        sock.close()
        raise RuntimeError("SOCKS5 不接受认证方式")

    try:
        ip = socket.inet_aton(host)
        addr = b"\x01" + ip
    except OSError:
        addr = b"\x03" + bytes([len(host)]) + host.encode()
    sock.sendall(b"\x05\x01\x00" + addr + struct.pack(">H", port))
    head = sock.recv(4)
    if len(head) < 4 or head[1] != 0:
        sock.close()
        raise RuntimeError("SOCKS5 连接目标失败")
    atyp = head[3]
    if atyp == 0x01:
        sock.recv(4)
    elif atyp == 0x03:
        sock.recv(sock.recv(1)[0])
    elif atyp == 0x04:
        sock.recv(16)
    sock.recv(2)
    return sock


def open_target(host, port):
    # 本机地址永远直连，避免把管理页/客户页也绕进代理
    if host in ("127.0.0.1", "localhost", "::1"):
        return socket.create_connection((host, port), timeout=20), b""
    up = parse_upstream(load_upstream())
    if not up:
        return socket.create_connection((host, port), timeout=20), b""
    if up["scheme"].startswith("socks"):
        return _open_via_socks(up, host, port)
    return _open_via_http_proxy(up, host, port, tls=(up["scheme"] == "https"))


def _pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 60)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 服务
# --------------------------------------------------------------------------- #
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(30)
        try:
            head = _recv_until(client)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = first.split()
            if len(parts) < 3:
                return
            method, target, _ = parts[0], parts[1], parts[2]

            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream, rest = open_target(host.strip("[]"), int(port))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    client.sendall(rest)
                _pipe(client, upstream)
                upstream.close()
                return

            # 普通 HTTP：绝对 URI → 拆成 origin-form
            if "://" not in target:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            u = urllib.parse.urlparse(target)
            port = u.port or (443 if u.scheme == "https" else 80)
            upstream = open_target(u.hostname, port)
            leftover = head.split(b"\r\n", 1)[1] if b"\r\n" in head else b""
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            upstream.sendall(("%s %s HTTP/1.1\r\n" % (method, path)).encode() + leftover)
            _pipe(client, upstream)
            upstream.close()
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    Server((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
