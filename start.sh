#!/usr/bin/env bash
# 启动 ChatGPT 账号管理台
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8899}"

# 本地代理中继（Chrome 固定连它，后台可切上游）
if ! nc -z 127.0.0.1 8898 2>/dev/null; then
  nohup python3 proxy_relay.py --port 8898 > relay.log 2>&1 &
  echo "已启动代理中继: 127.0.0.1:8898"
  sleep 0.6
fi

if curl -sS -m 1 --noproxy '*' "http://127.0.0.1:${PORT}/api/status" >/dev/null 2>&1; then
  echo "已在运行: http://127.0.0.1:${PORT}"
else
  nohup python3 admin_server.py --port "${PORT}" > server.log 2>&1 &
  echo "已启动: http://127.0.0.1:${PORT}  (日志 server.log)"
  sleep 1
fi

if [[ "$(uname)" == "Darwin" ]]; then
  open "http://127.0.0.1:${PORT}" || true
fi
