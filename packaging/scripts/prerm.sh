#!/usr/bin/env bash
# prerm.sh — C4 deb prerm / rpm %preun：停止并禁用服务（容忍已停止/未启用）。

set -euo pipefail

if command -v systemctl >/dev/null 2>&1; then
    systemctl stop c4-agent 2>/dev/null || true
    systemctl disable c4-agent 2>/dev/null || true
fi

exit 0
