#!/usr/bin/env bash
# postrm.sh — C4 deb postrm / rpm %postun：仅 purge 时删除 agent.env、账户与家目录。
#
# deb 语义：postrm 的第一个参数为 remove / upgrade / purge / failed-upgrade。
#   - purge：彻底清除 —— 删除 agent.env（含敏感密钥）、专用账户及其家目录。
#   - 其余：保留 agent.env 与 ~/.local/c4（设计 §6.2「默认保留」）。
# rpm 语义（内联于 .spec 的 %postun）：rpm 无原生 purge，默认一律保留。

set -euo pipefail

ACCOUNT="c4"
ACCOUNT_HOME="/home/c4"
ETC_DIR="/usr/local/etc/c4"

action="${1:-}"

if [ "$action" = "purge" ]; then
    rm -f "$ETC_DIR/agent.env"
    if id "$ACCOUNT" >/dev/null 2>&1; then
        userdel "$ACCOUNT" 2>/dev/null || true
    fi
    groupdel "$ACCOUNT" 2>/dev/null || true
    rm -rf "$ACCOUNT_HOME"
    rmdir "$ETC_DIR" 2>/dev/null || true
fi

exit 0
