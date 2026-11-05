#!/usr/bin/env bash
# 在服务器上安装 TokenDesk（幂等）。默认目录 /opt/gpt-buy。
#   bash deploy/install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/gpt-buy}"
cd "$(dirname "$0")/.."

echo ">> 应用目录: $APP_DIR"
mkdir -p "$APP_DIR"
# 同步代码（排除运行时数据与本地浏览器配置）
rsync -a --delete \
  --exclude '.git' --exclude '__pycache__' --exclude '.chrome-gpt-profile' \
  --exclude 'data' --exclude 'doc' --exclude '新建文本文档.txt' \
  --exclude '*.log' --exclude '*.png' \
  ./ "$APP_DIR/"

cd "$APP_DIR"
mkdir -p data data/shots
chmod 700 data

echo ">> 创建 venv 并安装依赖"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo ">> 安装 systemd 服务（监听 127.0.0.1）"
cp deploy/tokendesk-relay.service    /etc/systemd/system/
cp deploy/tokendesk-admin.service    /etc/systemd/system/
cp deploy/tokendesk-customer.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tokendesk-relay tokendesk-admin tokendesk-customer

echo ">> 完成。初始管理员密码见： $APP_DIR/data/初始密码.txt"
echo "   管理端  127.0.0.1:8930   客户端 127.0.0.1:8931   中继 127.0.0.1:8898"
echo "   对外访问请用 nginx + TLS 反代（见 deploy/nginx-tokendesk.conf），不要直接裸暴露端口。"
