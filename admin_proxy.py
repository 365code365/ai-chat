"""admin_proxy.py — 后台「代理」模块接口（混入 admin_server.Handler）。

    GET    /api/proxy                          渠道列表 + 默认渠道 + 中继状态 + 在用租约
    POST   /api/proxy/channels                 新建渠道
    PATCH  /api/proxy/channels/<id>            修改渠道（密码 / token 留空 = 不改）
    DELETE /api/proxy/channels/<id>            删除渠道
    POST   /api/proxy/channels/<id>/test       用该渠道新开一个 session 直连测试出口
    POST   /api/proxy/channels/<id>/traffic    kookeey 流量余额
    POST   /api/proxy/default      {id}        设默认渠道（"" = 直连）
    POST   /api/proxy/kookeey/auth {uid, accessid, token, channelId}  拉取提取模式账密
    POST   /api/proxy/rotate       {slot}      给某个浏览器换出口 IP（默认 main）
    POST   /api/proxy/test         {slot}      经中继检测某个浏览器当前出口
    POST   /api/proxy/restart                  重启管理端浏览器（新租约 ⇒ 新 IP）
"""
import re

import cgpt
import proxyhub
from browser import BROWSER, RELAY_PORT, relay_alive
from proxyhub import kookeey

SLOT_LABELS = {"main": "管理端浏览器"}


def _slot_label(slot):
    return SLOT_LABELS.get(slot) or ("客户开通 %s" % slot)


def _public_lease(lease):
    port = lease.get("cdpPort")
    return {
        "slot": lease.get("slot"),
        "label": _slot_label(lease.get("slot")),
        "channelId": lease.get("channelId") or "",
        "channelName": lease.get("channelName") or "直连",
        "rotation": lease.get("rotation") or "",
        "session": lease.get("session") or "",
        "startedAt": lease.get("startedAt"),
        "rotatedAt": lease.get("rotatedAt"),
        "jobId": lease.get("jobId") or "",
        "exit": lease.get("exit"),
        "running": bool(port) and cgpt.debugger_alive(port, timeout=0.2) is not None,
    }


class ProxyAdminMixin(object):
    # ---------- 读 ----------
    def proxy_payload(self):
        cfg = proxyhub.load_config()
        leases = [_public_lease(x) for x in proxyhub.list_leases()]
        leases.sort(key=lambda x: (x["slot"] != "main", x["slot"]))
        first_slot = proxyhub.slot_port("c0")
        return {
            "defaultId": cfg.get("defaultId") or "",
            "channels": [proxyhub.public_channel(c, cfg) for c in cfg["channels"]],
            "relay": {
                "alive": relay_alive(RELAY_PORT),
                "port": RELAY_PORT,
                "slotsAlive": relay_alive(first_slot),
                "slotBase": proxyhub.SLOT_BASE_PORT,
            },
            "leases": leases,
            "gateways": [{"host": h, "label": n} for h, n in kookeey.GATEWAYS],
        }

    # ---------- 写 ----------
    def api_proxy_post(self, path):
        body = self._body()
        if path == "/api/proxy/channels":
            return self.api_channel_save(None, body)
        m = re.match(r"^/api/proxy/channels/([^/]+)/(test|traffic)$", path)
        if m:
            return self.api_channel_action(m.group(1), m.group(2))
        if path == "/api/proxy/default":
            return self.api_proxy_default(body)
        if path == "/api/proxy/kookeey/auth":
            return self.api_kookeey_auth(body)
        if path == "/api/proxy/rotate":
            return self.api_proxy_rotate(body)
        if path == "/api/proxy/test":
            return self.api_proxy_test(body)
        if path == "/api/proxy/restart":
            return self.api_proxy_restart()
        return self._err("未知接口", 404)

    def api_channel_save(self, ch_id, body=None):
        body = self._body() if body is None else body
        cfg = proxyhub.load_config()
        old = proxyhub.get_channel(cfg, ch_id) if ch_id else None
        if ch_id and not old:
            return self._err("渠道不存在", 404)
        try:
            ch = proxyhub.validate_channel(body, old)
        except ValueError as exc:
            return self._err(str(exc))
        cfg["channels"] = [c for c in cfg["channels"] if c["id"] != ch["id"]] + [ch]
        cfg["channels"].sort(key=lambda c: c.get("createdAt") or 0)
        # 第一个渠道自动设为默认；也可在表单里勾「设为默认」
        if body.get("makeDefault") or len(cfg["channels"]) == 1 and not cfg.get("defaultId"):
            cfg["defaultId"] = ch["id"]
        proxyhub.save_config(cfg)
        return self._send(200, self.proxy_payload())

    def api_channel_delete(self, ch_id):
        cfg = proxyhub.load_config()
        if not proxyhub.get_channel(cfg, ch_id):
            return self._err("渠道不存在", 404)
        cfg["channels"] = [c for c in cfg["channels"] if c["id"] != ch_id]
        if cfg.get("defaultId") == ch_id:
            cfg["defaultId"] = ""
        proxyhub.save_config(cfg)
        return self._send(200, self.proxy_payload())

    def api_proxy_default(self, body):
        cfg = proxyhub.load_config()
        ch_id = body.get("id") or ""
        if ch_id and not proxyhub.get_channel(cfg, ch_id):
            return self._err("渠道不存在", 404)
        cfg["defaultId"] = ch_id
        proxyhub.save_config(cfg)
        return self._send(200, self.proxy_payload())

    def api_channel_action(self, ch_id, action):
        ch = proxyhub.get_channel(proxyhub.load_config(), ch_id)
        if not ch:
            return self._err("渠道不存在", 404)
        if action == "test":
            return self._send(200, proxyhub.probe_channel(ch))
        if ch["type"] != "kookeey":
            return self._err("只有 kookeey 渠道能查流量")
        try:
            return self._send(200, {"ok": True, **kookeey.traffic(ch)})
        except kookeey.KookeeyError as exc:
            return self._send(200, {"ok": False, "error": str(exc)})

    def api_kookeey_auth(self, body):
        """用开发者 ID + token 拉提取模式账密。token 没填就用已保存渠道里的。"""
        saved = proxyhub.get_channel(proxyhub.load_config(), body.get("channelId")) or {}
        ch = {
            "uid": re.sub(r"\D", "", str(body.get("uid") or saved.get("uid") or "")),
            "accessid": re.sub(r"\D", "", str(body.get("accessid") or saved.get("accessid") or "")),
            "token": (body.get("token") or "").strip() or saved.get("token") or "",
        }
        try:
            return self._send(200, {"ok": True, **kookeey.fetch_auth(ch)})
        except kookeey.KookeeyError as exc:
            return self._send(200, {"ok": False, "error": str(exc)})

    def _slot(self, body):
        slot = body.get("slot") or "main"
        proxyhub.slot_port(slot)      # 校验槽位名，不合法抛 ValueError
        return slot

    def api_proxy_rotate(self, body):
        try:
            slot = self._slot(body)
            lease, note = proxyhub.rotate(slot)
        except (ValueError, RuntimeError) as exc:
            return self._err(str(exc))
        info = proxyhub.probe_slot(slot) if lease.get("channelId") else {"ok": True, "ip": "直连"}
        return self._send(200, {"ok": True, "note": note, "exit": info,
                                "lease": _public_lease(proxyhub.load_lease(slot) or lease)})

    def api_proxy_test(self, body):
        try:
            slot = self._slot(body)
        except ValueError as exc:
            return self._err(str(exc))
        if not relay_alive(proxyhub.slot_port(slot)):
            return self._err("中继没在跑（或是旧版没有分槽位端口），先重启 proxy_relay.py")
        return self._send(200, proxyhub.probe_slot(slot))

    def api_proxy_restart(self):
        BROWSER.restart()
        return self._send(200, {"ok": True, "status": self.status_payload(),
                                "proxy": self.proxy_payload()})
