#!/usr/bin/env bash
# install.sh — C4 tar.gz 自包含安装器（独立版 postinst，幂等）。
#
# 用法：
#   sudo tar -xzf c4-<version>.tar.gz -C /usr/local
#   sudo /usr/local/lib/c4/scripts/install.sh
#
# 与 scripts/lib.sh 的常量保持一致（ACCOUNT=c4 / ACCOUNT_HOME=/home/c4）。
# tar.gz 以 /usr/local 为根，无法直接携带 /etc/systemd/system 下的单元文件，
# 因此本脚本内嵌 systemd 单元（内容与 systemd/c4-agent.service 完全一致）并写入。

set -euo pipefail

ACCOUNT="c4"
ACCOUNT_HOME="/home/c4"
BIN_DIR="/usr/local/bin"
LIB_DIR="/usr/local/lib/c4"
ETC_DIR="/usr/local/etc/c4"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_UNIT_NAME="c4-agent.service"

# 1. 创建账户（若不存在）
if ! getent passwd "$ACCOUNT" >/dev/null 2>&1; then
    useradd --system --home-dir "$ACCOUNT_HOME" --create-home \
        --shell /usr/sbin/nologin "$ACCOUNT"
fi

# 2. 运行时目录骨架（幂等）
install -d -o "$ACCOUNT" -g "$ACCOUNT" -m 700 "$ACCOUNT_HOME/.local/c4"
install -d -o "$ACCOUNT" -g "$ACCOUNT" -m 700 \
    "$ACCOUNT_HOME/.local/c4/state" \
    "$ACCOUNT_HOME/.local/c4/logs"
install -d -o "$ACCOUNT" -g "$ACCOUNT" -m 755 /var/log/c4/agent

# 3. 配置属主 + agent.env（仅当不存在时创建，绝不覆盖已有密钥文件）
# 注意：必须先创建 agent.env 再 chown，否则新建文件会落到 root:root。
if [ ! -f "$ETC_DIR/agent.env" ]; then
    if [ -f "$ETC_DIR/agent.env.example" ]; then
        cp "$ETC_DIR/agent.env.example" "$ETC_DIR/agent.env"
    else
        : > "$ETC_DIR/agent.env"
    fi
fi
chown -R root:"$ACCOUNT" "$ETC_DIR"
chmod 640 "$ETC_DIR/agent.env"

# 4. 安装 systemd 单元（内嵌，与 systemd/c4-agent.service 一致；不自动 start / enable）
install -d -m 0755 "$SYSTEMD_DIR"
cat > "$SYSTEMD_DIR/$SERVICE_UNIT_NAME" <<EOF
[Unit]
Description=C4 Agent
After=network.target

[Service]
User=$ACCOUNT
Group=$ACCOUNT
ExecStart=$LIB_DIR/agent/node/bin/node $LIB_DIR/agent/dist/index.js --config-dir $ACCOUNT_HOME/.local/c4
Restart=on-failure
RestartSec=5
EnvironmentFile=$ETC_DIR/agent.env
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=$ACCOUNT_HOME/.local/c4 /dev/shm /var/log/c4
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$SYSTEMD_DIR/$SERVICE_UNIT_NAME"

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
fi

exit 0
