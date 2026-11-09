#!/usr/bin/env bash
# 本机一键发布到线上：语法检查 → rsync 增量同步 → 按改动重启服务 → 健康检查
#   bash deploy/deploy.sh         正式发布
#   bash deploy/deploy.sh --dry   只列出将同步的文件，不改服务器
# 连接信息放在 deploy/.deploy.env（已 gitignore），需含 DEPLOY_HOST、SSHPASS。
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=deploy/.deploy.env
[[ -f $ENV_FILE ]] && source "$ENV_FILE"
: "${DEPLOY_HOST:?缺少 DEPLOY_HOST（写在 $ENV_FILE）}"
: "${SSHPASS:?缺少 SSHPASS（写在 $ENV_FILE）}"
export SSHPASS
DEPLOY_USER="${DEPLOY_USER:-root}"
APP_DIR="${APP_DIR:-/opt/gpt-buy}"
REMOTE="$DEPLOY_USER@$DEPLOY_HOST"
SSH="ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
DRY=""; [[ "${1:-}" == "--dry" ]] && DRY="-n"

echo ">> 语法检查"
find . -name '*.py' -not -path './.chrome-gpt-profile/*' -not -path './data/*' \
  -not -path './.venv/*' -not -path './doc/*' -print0 | xargs -0 python3 -m py_compile

echo ">> 同步到 $REMOTE:$APP_DIR ${DRY:+(dry-run)}"
CHANGES=$(rsync -az $DRY --out-format='%n' \
  --exclude '.git' --exclude '.kiro' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.chrome-gpt-profile' --exclude 'data' --exclude 'doc' \
  --exclude '新建文本文档.txt' --exclude '*.log' --exclude '*.png' \
  --exclude '.DS_Store' --exclude 'deploy/.deploy.env' \
  -e "sshpass -e $SSH" ./ "$REMOTE:$APP_DIR/" | grep -v '/$' || true)

if [[ -z $CHANGES ]]; then echo "   无文件变化，跳过重启"; exit 0; fi
echo "$CHANGES" | sed 's/^/   /'
[[ -n $DRY ]] && exit 0

# 按改动决定要做什么
REQ=0; SVC=0; RESTART=""
add() { [[ " $RESTART " == *" $1 "* ]] || RESTART="$RESTART $1"; }
while IFS= read -r f; do
  case "$f" in
    requirements.txt)      REQ=1; add tokendesk-admin; add tokendesk-customer ;;
    deploy/*.service)      SVC=1 ;;
    deploy/*)              ;;
    proxy_relay.py)        add tokendesk-relay ;;
    web/*|admin_*.py)      add tokendesk-admin ;;
    web_customer/*|customer_server.py) add tokendesk-customer ;;
    *.py)                  add tokendesk-admin; add tokendesk-customer ;;
  esac
done <<< "$CHANGES"
[[ $SVC == 1 ]] && { add tokendesk-relay; add tokendesk-admin; add tokendesk-customer; }

if [[ -z $RESTART && $REQ == 0 ]]; then echo ">> 无需重启服务"; exit 0; fi
echo ">> 远端：依赖=$REQ 服务文件=$SVC 重启:${RESTART:- 无}"
sshpass -e $SSH "$REMOTE" bash -s <<EOF
set -e
cd "$APP_DIR"
if [ "$REQ" = 1 ]; then ./.venv/bin/pip install -q -r requirements.txt; fi
if [ "$SVC" = 1 ]; then cp deploy/tokendesk-*.service /etc/systemd/system/; systemctl daemon-reload; fi
if [ -n "$RESTART" ]; then
  systemctl restart $RESTART
  sleep 3
  for s in $RESTART; do
    if systemctl is-active --quiet \$s; then echo "   OK  \$s"
    else echo "   FAIL \$s"; journalctl -u \$s -n 40 --no-pager; exit 1; fi
  done
fi
echo "   客户端 HTTP \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8931/)  管理端 HTTP \$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8930/)"
EOF
echo ">> 发布完成"
