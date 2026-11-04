# TokenDesk

ChatGPT 订阅自动化工具，分两套完全隔离的页面：

| 端口 | 页面 | 主题 | 登录 | 使用者 |
|---|---|---|---|---|
| `8899` | 管理后台 `/` | 浅色 | admin / 密码（见下） | 你自己：管账号、管卡、发兑换码 |
| `8900` | 客户兑换页 `/` | 浅色 | 兑换码 | 客户：输兑换码 → 贴登录信息 → 一键订阅 |

### 管理端登录

首次启动自动生成密码，写入 `data/初始密码.txt`，同时后台日志会打印一行。
也可以自己指定：

```bash
ADMIN_USER=admin ADMIN_PASSWORD=你的密码 python3 admin_server.py --port 8899
```

改密码：删掉 `data/auth.json` 重启（会重新生成），或直接用上面的环境变量。
密码用 PBKDF2-SHA256（12 万次）加盐存储，会话是 HttpOnly Cookie，12 小时过期，
并持久化在 `data/sessions.json`（0600）——**重启服务不用重新登录**。

![管理后台](docs/ui_accounts.png)

## 客户流程（亮色 / 独立端口）

1. 客户打开 `http://<host>:8900`
2. 输入兑换码 → 命中对应卡（只显示 `Visa ···· 4242`，客户看不到完整卡号）
3. 选 Plus / Pro
4. 点「从当前浏览器自动获取」——后台会用受控 Chrome 打开 `chatgpt.com/api/auth/session`，把 JSON 读回来自动填入（也可以点旁边的手动链接自己复制）
5. 点「开始订阅」，后台自动完成：登录 → 打开订阅页 → 进付款页 → 填卡 → 填账单地址 → 提交

![客户第二步](docs/customer_step2.png)
![客户进度](docs/customer_step3.png)

## 后台卡库与兑换码

- 每张卡自动生成一个兑换码（`AB3K-9PQD-M2` 形式），可复制、可重置（重置后旧码立即失效）
- **一张卡可以绑定多个账号**（卡库里用 chips 展示，勾选框增删）
- 卡行有「复制卡」按钮，一次复制 `卡号 / 有效期 / CVC`，可直接粘到支付表单
- 客户页只按兑换码取卡，不暴露账号
- 兑换成功后标记「已兑换」，状态留在卡库里

## 代理（拿地区价）

后台侧边栏「代理」可填上游代理，登录/订阅/结账所有流量都会走它：

```
浏览器 → 127.0.0.1:8898（本地中继）→ 你填的上游（http / https / socks5）→ 目标
```

- Chrome 只在启动时读 `--proxy-server`，所以**第一次启用要点「重启浏览器并应用」**；之后改上游只改配置，中继按连接实时读取，不用再重启
- 支持三种写法：
  - `host:port:user:pass`（Kookeey 等代理商格式，默认按 HTTP 代理）
  - `http://user:pass@host:port` / `https://...`
  - `socks5://user:pass@host:port`
- 留空 = 直连
- 「检测出口 IP」会让中继去请求 ipify，显示真正的出口 IP 和延迟
- `127.0.0.1` 走 `--proxy-bypass-list=<-loopback>` 直连，不会被代理

```bash
python3 proxy_relay.py --port 8898      # 手动起中继（start.sh 会自动起）
```

> 币种由 IP 区域 + 账号账单地区共同决定；换 IP 后要重新打开订阅页才会刷新价格。
> 卡国家与 IP 差异过大时 Stripe 可能直接拒付。

**实测记录（2026-11）**：`gate.kookeey.info:1000` 香港节点可以连通（HTTP 515ms / SOCKS5 435ms，
出口 119.236.157.37），但访问 chatgpt.com 被直接拦截：

```
Unable to load site
Please try again later. If you are using a VPN, try turning it off.
[IP:119.236.157.37]
```

结论：机房 IP 会被 ChatGPT 识别为 VPN。要拿地区价，需要住宅/移动出口，且最好与账号账单地区一致。

## 启动

```bash
python3 proxy_relay.py --port 8898      # 代理中继（可选，start.sh 会起）
python3 admin_server.py --port 8899     # 后台
python3 customer_server.py --port 8900  # 客户页
```

## 代码结构（单文件均 < 500 行）

```
cgpt/                  # 自动化核心（原 cgpt.py 拆分）
  config.py            常量
  cdp.py               CDP 连接 / 浏览器进程 / 截图
  auth.py              token 解析、cookie 分块注入、登录
  plans.py             定价页、套餐弹窗、结账页跳转
  cards.py             Stripe 卡信息与账单地址填写
store.py               账号 / 卡库 / 兑换码 持久化
browser.py             共享 Chrome 单例
admin_server.py        后台 API + 静态服务（629 行）
customer_server.py     客户页 API + 订阅任务队列（311 行）
web/                   后台前端（深色）
web_customer/          客户前端（浅色）
```

## 已知边界

- 提交后按页面文案判断结果（成功/被拒/待验证），Stripe 的 3DS 仍需人工在窗口完成
- 客户页与后台共享同一个 Chrome 配置，同一时刻只能处理一个订阅任务（有队列锁）
- 后台前端 `web/app.js` 812 行、`style.css` 610 行，仍可继续按视图拆分
