#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proxy_relay.py — 本地代理中继（按浏览器分槽位）

每个 Chrome 实例连中继上自己的端口，中继据端口知道连接属于哪个浏览器（槽位），
再按 proxyhub 里该槽位的租约（渠道 + session）选上游：
    127.0.0.1:8898        管理端浏览器（槽位 main）
    127.0.0.1:8960+i      客户池浏览器 c{i}

渠道/租约变化按连接实时生效；租约换代（换 IP）时，中继会主动断开该槽位的旧隧道，
浏览器重连后即走新 IP。对 HTTPS 只做 CONNECT 隧道，不碰证书。

启动:
    python3 proxy_relay.py --port 8898
"""
import argparse
import itertools
import select
import socket
import socketserver
import threading
import time
import urllib.parse

import proxyhub
from proxyhub.tunnel import open_via, _read_head as read_head

DEFAULT_PORT = proxyhub.RELAY_PORT
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# 活动隧道：id -> (槽位, 租约代数, [sockets])
_conns = {}
_conns_lock = threading.Lock()
_ids = itertools.count(1)


def open_target(host, port, slot="main"):
    """返回 (已连通目标的 socket, 上游多读到的数据, 租约代数)。"""
    if host in LOCAL_HOSTS:
        return socket.create_connection((host, port), timeout=20), b"", None
    up, gen = proxyhub.resolve(slot)
    sock, rest = open_via(up, host, port, timeout=20)
    sock.settimeout(None)
    return sock, rest, gen


def _pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass


def _watch_stale():
    """每秒检查一次：租约换代的槽位，断开旧隧道（浏览器会自动重连走新 IP）。"""
    while True:
        time.sleep(1)
        with _conns_lock:
            items = list(_conns.values())
        current = {}
        for slot, gen, socks in items:
            if gen is None:
                continue
            if slot not in current:
                try:
                    current[slot] = proxyhub.lease_gen(slot)
                except Exception:
                    current[slot] = gen
            if current[slot] != gen:
                for s in socks:
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(30)
        slot = self.server.slot
        cid = None
        upstream = None
        try:
            head, rest_client = read_head(client)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1")
            parts = first.split()
            if len(parts) < 3:
                return
            method, target = parts[0], parts[1]

            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream, rest, gen = open_target(host.strip("[]"), int(port), slot)
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    client.sendall(rest)
                if rest_client:
                    upstream.sendall(rest_client)
            else:
                # 普通 HTTP：绝对 URI → 拆成 origin-form
                if "://" not in target:
                    client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    return
                u = urllib.parse.urlparse(target)
                port = u.port or (443 if u.scheme == "https" else 80)
                upstream, rest, gen = open_target(u.hostname, port, slot)
                leftover = head.split(b"\r\n", 1)[1] if b"\r\n" in head else b""
                path = u.path or "/"
                if u.query:
                    path += "?" + u.query
                headers = leftover + b"\r\n" if leftover else b""
                upstream.sendall(("%s %s HTTP/1.1\r\n" % (method, path)).encode()
                                 + headers + b"\r\n" + rest_client)
                if rest:
                    client.sendall(rest)

            client.settimeout(None)
            cid = next(_ids)
            with _conns_lock:
                _conns[cid] = (slot, gen, [client, upstream])
            _pipe(client, upstream)
        except Exception as exc:
            try:
                msg = str(exc).encode("utf-8", "replace")[:200]
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\n"
                               b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(msg), msg))
            except Exception:
                pass
        finally:
            if cid is not None:
                with _conns_lock:
                    _conns.pop(cid, None)
            for s in (upstream, client):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, slot):
        self.slot = slot
        socketserver.ThreadingTCPServer.__init__(self, addr, handler)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help="管理端浏览器（槽位 main）端口；客户池槽位固定用 %d 起" % proxyhub.SLOT_BASE_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    servers = []
    for port, slot in proxyhub.relay_slots():
        if slot == "main":
            port = args.port
        try:
            servers.append(Server((args.host, port), Handler, slot))
        except OSError as exc:
            if slot == "main":
                raise
            print("槽位 %s 端口 %d 监听失败：%s" % (slot, port, exc))
    threading.Thread(target=_watch_stale, daemon=True).start()
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("代理中继: %s:%d (main) + %d 个客户槽位 (%d 起)"
          % (args.host, args.port, len(servers) - 1, proxyhub.SLOT_BASE_PORT))
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
