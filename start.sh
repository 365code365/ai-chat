#!/usr/bin/env bash
# 启动 ChatGPT 账号管理台
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8899}"

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
