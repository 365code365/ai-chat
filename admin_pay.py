"""admin_pay.py — 后台「支付」模块接口（混入 admin_server.Handler）。

    GET  /api/pay                   配置（密钥只给是否已填）+ 最近订单 + 回调地址
    POST /api/pay/config            保存渠道配置（密钥留空=不改）
    POST /api/pay/products          保存套餐价格表
    POST /api/pay/order/test {channel}        发起 ¥0.01 联调测试单，返回二维码
    GET  /api/pay/orders            订单列表
    POST /api/pay/order/query {token}         主动查单刷新状态
"""
import re

import payhub


def _order_view(o):
    return {
        "id": o.get("id"),
        "outTradeNo": o.get("outTradeNo"),
        "channel": o.get("channel"),
        "productId": o.get("productId"),
        "plan": o.get("plan"),
        "amountYuan": "%.2f" % (int(o.get("amount") or 0) / 100.0),
        "subject": o.get("subject"),
        "email": o.get("email"),
        "contact": o.get("contact"),
        "status": o.get("status"),
        "test": bool(o.get("test")),
        "providerTradeNo": o.get("providerTradeNo"),
        "createdAt": o.get("createdAt"),
        "paidAt": o.get("paidAt"),
        "activateJobId": o.get("activateJobId"),
    }


class PayAdminMixin(object):
    def pay_payload(self):
        cfg = payhub.load_config()
        pub = payhub.public_config(cfg)
        base = (cfg.get("notifyBase") or "").rstrip("/")
        pub["notify"] = {
            "alipay": (base + "/api/pay/alipay/notify") if base else "",
            "wechat": (base + "/api/pay/wechat/notify") if base else "",
        }
        pub["orders"] = [_order_view(o) for o in payhub.list_orders(100)]
        return pub

    def api_pay(self):
        return self._send(200, self.pay_payload())

    def api_pay_post(self, path):
        if path == "/api/pay/config":
            return self.api_pay_save()
        if path == "/api/pay/products":
            return self.api_pay_products()
        if path == "/api/pay/order/test":
            return self.api_pay_test_order()
        if path == "/api/pay/order/query":
            return self.api_pay_order_query()
        return self._err("未知接口", 404)

    def api_pay_save(self):
        body = self._body()
        cfg = payhub.load_config()
        if isinstance(body.get("notifyBase"), str):
            cfg["notifyBase"] = body["notifyBase"].strip().rstrip("/")
        for ch in payhub.CHANNELS:
            incoming = body.get(ch)
            if not isinstance(incoming, dict):
                continue
            cur = cfg[ch]
            if "enabled" in incoming:
                cur["enabled"] = bool(incoming["enabled"])
            # 普通字段直接覆盖
            for f in ("appId", "gateway", "mchId", "serialNo"):
                if f in incoming:
                    cur[f] = (incoming[f] or "").strip()
            # 密钥字段：留空=保持原值
            for f in payhub._SECRET_FIELDS[ch]:
                if incoming.get(f):
                    cur[f] = incoming[f].strip()
        payhub.save_config(cfg)
        return self._send(200, self.pay_payload())

    def api_pay_products(self):
        """保存首页套餐。price 单位：分。顺序即首页展示顺序。"""
        body = self._body()
        items = body.get("products")
        if not isinstance(items, list) or not items:
            return self._err("套餐列表不能为空")
        if len(items) > 20:
            return self._err("套餐最多 20 个")
        cleaned, seen = [], set()
        for i, p in enumerate(items, 1):
            if not isinstance(p, dict):
                return self._err("第 %d 个套餐格式不对" % i)
            title = str(p.get("title") or "").strip()
            label = title or "第 %d 个套餐" % i
            pid = str(p.get("id") or "").strip()
            plan = str(p.get("plan") or "").strip().lower()
            if not title:
                return self._err("第 %d 个套餐没填标题" % i)
            if not re.match(r"^[A-Za-z0-9_-]{1,32}$", pid):
                return self._err("「%s」的编号只能是字母、数字、- 或 _" % label)
            if pid in seen:
                return self._err("套餐编号重复：%s" % pid)
            if plan not in ("plus", "pro"):
                return self._err("「%s」的开通类型只能是 Plus 或 Pro" % label)
            try:
                price = int(round(float(p.get("price"))))
            except Exception:
                return self._err("「%s」价格不合法" % label)
            if not 1 <= price <= 10000000:
                return self._err("「%s」价格必须在 0.01 ~ 100000 元之间" % label)
            bullets = p.get("bullets") or []
            if isinstance(bullets, str):
                bullets = bullets.splitlines()
            bullets = [str(b).strip()[:80] for b in bullets if str(b).strip()][:15]
            item = {"id": pid, "plan": plan, "price": price, "bullets": bullets,
                    "enabled": bool(p.get("enabled", True)), "recommend": bool(p.get("recommend"))}
            for key, limit in payhub.PRODUCT_TEXT_LIMITS.items():
                item[key] = str(p.get(key) or "").strip()[:limit]
            seen.add(pid)
            cleaned.append(payhub.normalize_product(item))
        if not any(p["enabled"] for p in cleaned):
            return self._err("至少要上架一个套餐")
        cfg = payhub.load_config()
        cfg["products"] = cleaned
        payhub.save_config(cfg)
        return self._send(200, self.pay_payload())

    def api_pay_test_order(self):
        body = self._body()
        channel = body.get("channel")
        order, res = payhub.create_order(channel, "test", test=True)
        if not res.get("ok"):
            return self._err(res.get("error") or "下单失败")
        return self._send(200, {"ok": True, "qr": res["qr"], "qrImg": payhub.qr_data_uri(res["qr"]),
                                "token": order["token"], "outTradeNo": order["outTradeNo"],
                                "amountYuan": "0.01", "channel": channel})

    def api_pay_order_query(self):
        body = self._body()
        order = payhub.query_and_sync(body.get("token") or "")
        if not order:
            return self._err("订单不存在", 404)
        return self._send(200, {"ok": True, "order": _order_view(order)})
