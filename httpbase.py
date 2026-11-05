"""httpbase.py — 后台与客户页共用的 HTTP 基础设施。"""
import hashlib
import json
import mimetypes
import os
import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class JSONHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        extra = extra or {}
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _err(self, msg, code=400):
        self._send(code, {"error": str(msg)})

    def _path(self):
        return self.path.split("?")[0]

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _static(self, web_dir, path, extra=None):
        """从 web_dir 发静态文件；拒绝跳出目录（含 web → web_customer 这种同前缀目录）。

        缓存策略：
          * HTML 里引用的 /xxx.js、/xxx.css 自动加 ?v=内容哈希；带 ?v= 的请求长期缓存
            （内容一变哈希就变，部署后自动拿新版，不会用到旧缓存）。
          * HTML 本身 no-cache + ETag，再次打开只回 304，不重传。
        """
        if path in ("/", ""):
            path = "/index.html"
        root = os.path.abspath(web_dir)
        full = os.path.normpath(os.path.join(root, path.lstrip("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._err("Not found", 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"

        body, etag = _cached_file(full)
        if full.endswith(".html"):
            body = _version_assets(root, body)
            etag = '"%s"' % hashlib.sha1(body).hexdigest()[:16]
        headers = dict(extra or {})
        headers["ETag"] = etag
        if "v=" in (self.path.split("?", 1)[1] if "?" in self.path else ""):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            headers.setdefault("Cache-Control", "no-cache")
        if _etag_match(self.headers.get("If-None-Match"), etag):
            self.send_response(304)
            for k in ("ETag", "Cache-Control"):
                self.send_header(k, headers[k])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self._send(200, body, ctype, extra=headers)


# --------------------------------------------------------------------------- #
# 静态文件缓存：按 (mtime, size) 失效，避免每个请求都读盘、算哈希
# --------------------------------------------------------------------------- #
_file_cache = {}
_file_cache_lock = threading.Lock()
_ASSET_REF = re.compile(rb'((?:src|href)=")(/[A-Za-z0-9_./-]+\.(?:js|css))(")')


def _cached_file(full):
    st = os.stat(full)
    key = (st.st_mtime_ns, st.st_size)
    with _file_cache_lock:
        hit = _file_cache.get(full)
        if hit and hit[0] == key:
            return hit[1], hit[2]
    with open(full, "rb") as f:
        body = f.read()
    etag = '"%s"' % hashlib.sha1(body).hexdigest()[:16]
    with _file_cache_lock:
        _file_cache[full] = (key, body, etag)
    return body, etag


def _version_assets(root, html):
    """把 HTML 里的 /app.js、/style.css 换成 /app.js?v=<内容哈希>。"""
    def repl(m):
        asset = os.path.normpath(os.path.join(root, m.group(2).decode().lstrip("/")))
        if not asset.startswith(root + os.sep) or not os.path.isfile(asset):
            return m.group(0)
        _, etag = _cached_file(asset)
        return m.group(1) + m.group(2) + b"?v=" + etag.strip('"').encode()[:10] + m.group(3)
    return _ASSET_REF.sub(repl, html)


def _etag_match(header, etag):
    """If-None-Match 比较；兼容 nginx gzip 后改成的弱 ETag（W/"..."）。"""
    if not header:
        return False
    want = etag.strip('"')
    return any(t.strip().lstrip("W/").strip('"') == want for t in header.split(","))
