#!/usr/bin/env bash
# postinst.sh — C4 deb postinst / rpm %post 逻辑（幂等）。
#
# 与 scripts/lib.sh 的常量保持一致（ACCOUNT=c4 / ACCOUNT_HOME=/home/c4）。
# 行为（设计 §6.1）：
#   1. 创建专用账户（若不存在）
#   2. 创建运行时目录骨架 ~/.local/c4/{state,logs}
#   3. 应用 /usr/local/etc/c4 属主 root:c4；仅当 agent.env 不存在时从模板创建
#   4. systemd 单元由包文件本身安装（/etc/systemd/system/c4-agent.service），
#      此处仅 daemon-reload；不自动 start、不自动 enable。

set -euo pipefail

ACCOUNT="c4"
ACCOUNT_HOME="/home/c4"
ETC_DIR="/usr/local/etc/c4"

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

# 4. 刷新 systemd（单元由包文件提供；不自动 start / 不自动 enable）
if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload || true
fi

exit 0
