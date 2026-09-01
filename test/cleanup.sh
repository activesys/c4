#!/usr/bin/env bash
# cleanup.sh — 清理 C4 运行产生的文件并重启 c4-agent，恢复到干净首次接入态。
#
# 用法：
#   sudo ./cleanup.sh [--site-name 名称] [--site-abbr 缩写]
#
# 清理范围：
#   - ~/.local/c4/config.json + config.json.bak（接入配置）
#   - ~/.local/c4/abbr_registry.json（abbr 记忆库）
#   - /dev/shm/<instance_id>（POSIX 共享内存，instance_id 取自 agent.json）
#   - state/ 与 logs/ 目录内容
#   - agent.json 的 site 字段重置（默认 华能阿拉善/hnals，可用参数覆盖）
#
# 清理后自动重启 c4-agent 并自检（服务 active + /api/services 200）。

set -euo pipefail

C4_DIR="${C4_DIR:-/home/c4/.local/c4}"
SERVICE="${SERVICE:-c4-agent}"
SITE_NAME="华能阿拉善"
SITE_ABBR="hnals"

while [ $# -gt 0 ]; do
    case "$1" in
        --site-name) SITE_NAME="$2"; shift 2 ;;
        --site-abbr) SITE_ABBR="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,14p' "$0"
            exit 0
            ;;
        *) echo "未知参数: $1" >&2; exit 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "错误: 请用 sudo 运行（需要清理 c4 账户文件与共享内存）" >&2
    exit 1
fi

AGENT_JSON="$C4_DIR/agent.json"
step() { echo "[cleanup] $*"; }

# ── 1. 停止服务 ────────────────────────────────────────────
step "停止 $SERVICE"
systemctl stop "$SERVICE" || true

# Agent 经 Stop-Start 拉起的 MCP 服务进程不随 agent 退出，
# 残留会破坏「首次启动」语义（startup 测试 / 复测环境），一并清理
step "清理残留 MCP 服务进程"
pkill -f '/usr/local/bin/c4_' 2>/dev/null || true
sleep 1

# ── 2. 读取 instance_id（共享内存名）────────────────────────
INSTANCE_ID="c4_main"
if [ -f "$AGENT_JSON" ]; then
    INSTANCE_ID="$(python3 -c "
import json
print(json.load(open('$AGENT_JSON')).get('instance_id', 'c4_main'))
" 2>/dev/null || echo c4_main)"
fi
step "实例: $INSTANCE_ID"

# ── 3. 清理生成文件 ────────────────────────────────────────
step "删除接入配置 / 记忆库 / 共享内存"
rm -f "$C4_DIR/config.json" \
      "$C4_DIR/config.json.bak" \
      "$C4_DIR/abbr_registry.json" \
      "/dev/shm/$INSTANCE_ID"

step "清空 state/ 与 logs/ 目录内容"
find "$C4_DIR/state" "$C4_DIR/logs" -type f -delete 2>/dev/null || true

# ── 4. 重置 agent.json 的 site 字段 ────────────────────────
if [ -f "$AGENT_JSON" ]; then
    step "重置 site → $SITE_NAME / $SITE_ABBR"
    python3 -c "
import json
p = '$AGENT_JSON'
cfg = json.load(open(p))
cfg['site'] = {'name': '$SITE_NAME', 'abbr': '$SITE_ABBR'}
with open(p, 'w') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=4)
    f.write('\n')
"
else
    step "警告: $AGENT_JSON 不存在，跳过 site 重置"
fi

# ── 5. 启动服务并自检 ──────────────────────────────────────
step "启动 $SERVICE"
systemctl start "$SERVICE"

PORT="9988"
if [ -f "$AGENT_JSON" ]; then
    PORT="$(python3 -c "
import json
print(json.load(open('$AGENT_JSON')).get('server', {}).get('port', 9988))
" 2>/dev/null || echo 9988)"
fi

ok=""
for _ in $(seq 1 30); do
    if [ "$(systemctl is-active "$SERVICE")" = "active" ] \
       && curl -s -o /dev/null -w '' "http://127.0.0.1:$PORT/api/services" 2>/dev/null; then
        if curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/services" | grep -q 200; then
            ok="1"
            break
        fi
    fi
    sleep 1
done

if [ -z "$ok" ]; then
    echo "[cleanup] 错误: 服务未能就绪，请检查 journalctl -u $SERVICE" >&2
    exit 1
fi

step "完成 — 服务 active，环境已恢复到干净首次接入态"
ls -la "$C4_DIR" | grep -vE "^total|^d" || true
