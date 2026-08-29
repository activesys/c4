#!/usr/bin/env bash
# lib.sh — C4 打包共享布局常量与辅助函数。
#
# 被 build.sh / verify.sh 等构建侧脚本 source。所有安装布局常量在此统一定义，
# 作为「单一事实来源」；生命周期脚本（postinst/prerm/postrm/install.sh）内联的
# 默认值必须与此处保持一致（默认账户 c4，见设计 c4_deployment.md §5.1）。

set -euo pipefail

# ── 安装布局常量（设计 §5.1）──────────────────────────────
BIN_DIR="/usr/local/bin"
LIB_DIR="/usr/local/lib/c4"
ETC_DIR="/usr/local/etc/c4"
ACCOUNT="c4"
ACCOUNT_HOME="/home/c4"

# ── 版本与架构 ─────────────────────────────────────────────
# 版本默认读 VERSION 文件，但允许 build.sh --version= 或环境变量覆盖；
# 此处在 source 时仅作兜底。
PKG_VERSION="${PKG_VERSION:-1.0.0}"
PKG_ARCH="${PKG_ARCH:-amd64}"
RPM_ARCH="${RPM_ARCH:-x86_64}"
CODENAME="C4H1"

# ── 组件清单 ───────────────────────────────────────────────
MCP_SERVICES="c4_shm_manager c4_modbus_client c4_iec104_client c4_asfp2_client c4_asfp2_server c4_influxdb_client"
REGISTRY_SRC_DIR="config/mcp-registry"
SERVICE_UNIT_NAME="c4-agent.service"
SYSTEMD_DIR="/etc/systemd/system"

# ── 辅助函数 ───────────────────────────────────────────────
info() { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '[step %s/%s] %s\n' "$1" "$2" "$3"; }
