#!/usr/bin/env bash
# verify.sh — 结构化验证（仅静态结构检查；不做 sudo 安装，安装 QA 由调用方另行执行）。
#
# 检查对象：out/staging（构建产物树）与 out/pkg（三种成品包）。
# 输出 PASS <id> / FAIL <id> <reason>；任一 FAIL 则非零退出。

set -euo pipefail

# 统一 C locale：避免 ldd/file 等工具输出本地化（如中文「不是动态可执行文件」）
export LC_ALL=C
export LANG=C

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGING_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib.sh"

STAGING="$PACKAGING_DIR/out/staging"
OUT_PKG="$PACKAGING_DIR/out/pkg"

# 版本：build.sh 经环境变量 C4_BUILD_VERSION 传递（含 --version 覆盖）；否则读 VERSION 文件
PKG_VERSION="${C4_BUILD_VERSION:-$(tr -d '[:space:]' < "$PACKAGING_DIR/VERSION")}"

TOTAL_FAILS=0

# 每个检查块使用 _ok 标记自身成败；_fail 记录原因并置 _ok=0
_ok=1
_fail() {
    printf '    - %s\n' "$*"
    _ok=0
}

# ── V1：6 个二进制静态链接 + 权限 555 ───────────────────────
_ok=1
for _svc in $MCP_SERVICES; do
    _bin="$STAGING/usr/local/bin/$_svc"
    [ -e "$_bin" ] || { _fail "$_svc 缺失"; continue; }
    _mode="$(stat -c '%a' "$_bin")"
    [ "$_mode" = "555" ] || _fail "$_svc mode=$_mode (want 555)"
    grep -q "statically linked" <<< "$(file "$_bin")" || _fail "$_svc 非静态链接"
    _ldd="$(ldd "$_bin" 2>&1 || true)"
    grep -q "not a dynamic executable" <<< "$_ldd" || _fail "$_svc ldd 非静态"
done
if [ "$_ok" -eq 1 ]; then printf 'PASS V1\n'; else printf 'FAIL V1\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi

# ── V2：捆绑 node 版本为 v22.x ──────────────────────────────
_ok=1
_node="$STAGING/usr/local/lib/c4/agent/node/bin/node"
if [ ! -x "$_node" ]; then
    _fail "bundled node 缺失"
else
    _ver="$("$_node" --version 2>/dev/null || true)"
    case "$_ver" in
        v22.*) : ;;
        *) _fail "node 版本 '$_ver' 非 v22.x" ;;
    esac
fi
if [ "$_ok" -eq 1 ]; then printf 'PASS V2\n'; else printf 'FAIL V2\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi

# ── V3：system.txt 双路径 + dist/index.js ────────────────────
_ok=1
[ -f "$STAGING/usr/local/lib/c4/agent/dist/super_worker/prompts/system.txt" ] \
    || _fail "dist/super_worker/prompts/system.txt 缺失"
[ -f "$STAGING/usr/local/lib/c4/agent/src/super_worker/prompts/system.txt" ] \
    || _fail "src/super_worker/prompts/system.txt 缺失"
[ -f "$STAGING/usr/local/lib/c4/agent/dist/index.js" ] \
    || _fail "dist/index.js 缺失"
if [ "$_ok" -eq 1 ]; then printf 'PASS V3\n'; else printf 'FAIL V3\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi

# ── V4：前端 index.html 存在且引用 /assets/ ──────────────────
_ok=1
_idx="$STAGING/usr/local/lib/c4/frontend/index.html"
[ -f "$_idx" ] || _fail "frontend/index.html 缺失"
if [ -f "$_idx" ]; then
    grep -q '/assets/' "$_idx" || _fail "index.html 未引用 /assets/"
fi
if [ "$_ok" -eq 1 ]; then printf 'PASS V4\n'; else printf 'FAIL V4\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi

# ── V5：恰好 5 个注册表 JSON，binary_path 正确 ──────────────
_ok=1
_regdir="$STAGING/usr/local/etc/c4/mcp-registry"
if [ ! -d "$_regdir" ]; then
    _fail "mcp-registry 目录缺失"
else
    _n="$(find "$_regdir" -maxdepth 1 -name '*.json' -type f | wc -l)"
    [ "$_n" -eq 5 ] || _fail "注册表 JSON 数量=$_n (want 5)"
    for _f in "$_regdir"/*.json; do
        _name="$(basename "$_f" .json)"
        _bp="$(grep -o '"binary_path"[[:space:]]*:[[:space:]]*"[^"]*"' "$_f" \
            | head -1 | sed 's/.*:[[:space:]]*"//; s/"$//')"
        _want="/usr/local/bin/$_name"
        [ "$_bp" = "$_want" ] || _fail "$_name binary_path=$_bp (want $_want)"
    done
fi
if [ "$_ok" -eq 1 ]; then printf 'PASS V5\n'; else printf 'FAIL V5\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi

# ── V6：deb 存在 → 关键路径与权限、3 个脚本 ──────────────────
_ok=1
_deb="$OUT_PKG/c4_${PKG_VERSION}_${PKG_ARCH}.deb"
if [ ! -f "$_deb" ]; then
    _fail "deb 缺失 ($_deb)"
else
    _listing="$(dpkg-deb -c "$_deb")"
    for _svc in $MCP_SERVICES; do
        _line="$(echo "$_listing" | grep -E "\./usr/local/bin/$_svc$" || true)"
        [ -n "$_line" ] || { _fail "$_svc 未在 deb 中"; continue; }
        grep -q '^-r-xr-xr-x' <<< "$_line" || _fail "$_svc mode 非 555"
    done
    grep -q "\./etc/systemd/system/c4-agent.service$" <<< "$_listing" \
        || _fail "systemd 单元缺失"
    grep -q "\./usr/local/etc/c4/agent.env.example$" <<< "$_listing" \
        || _fail "agent.env.example 缺失"
    _tmp="$(mktemp -d)"
    dpkg-deb -e "$_deb" "$_tmp"
    for _s in postinst prerm postrm; do
        [ -f "$_tmp/$_s" ] || _fail "脚本 $_s 缺失"
    done
    rm -rf "$_tmp"
fi
if [ "$_ok" -eq 1 ]; then printf 'PASS V6\n'; else printf 'FAIL V6\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi

# ── V7：tar.gz 存在 → 关键路径 ──────────────────────────────
_ok=1
_tgz="$OUT_PKG/c4-${PKG_VERSION}.tar.gz"
if [ ! -f "$_tgz" ]; then
    _fail "tar.gz 缺失 ($_tgz)"
else
    _listing="$(tar -tzf "$_tgz")"
    for _p in ./bin/c4_shm_manager ./lib/c4/agent/dist/index.js ./lib/c4/scripts/install.sh; do
        grep -Fqx "$_p" <<< "$_listing" || _fail "缺失 $_p"
    done
    grep -q '^\./etc/c4/mcp-registry/' <<< "$_listing" || _fail "mcp-registry 缺失"
fi
if [ "$_ok" -eq 1 ]; then printf 'PASS V7\n'; else printf 'FAIL V7\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi

# ── V8：rpm（若产出）→ RPM 魔数 ──────────────────────────────
_rpm="$OUT_PKG/c4-${PKG_VERSION}.${RPM_ARCH}.rpm"
if [ ! -f "$_rpm" ]; then
    printf 'SKIP V8 (no rpm produced)\n'
else
    _ok=1
    file "$_rpm" | grep -qi 'RPM' || _fail "file 未识别为 RPM"
    _magic="$(head -c 4 "$_rpm" | od -An -tx1 | tr -d ' \n')"
    [ "$_magic" = "edabeedb" ] || _fail "魔数=$_magic (want edabeedb)"
    if [ "$_ok" -eq 1 ]; then printf 'PASS V8\n'; else printf 'FAIL V8\n'; TOTAL_FAILS=$((TOTAL_FAILS + 1)); fi
fi

if [ "$TOTAL_FAILS" -ne 0 ]; then
    echo "verify: $TOTAL_FAILS 项失败"
    exit 1
fi
echo "verify: all passed"
exit 0
