#!/usr/bin/env bash
# build.sh — C4 打包编排入口。
#
# 用法：
#   ./build.sh [--all|--rpm|--deb|--targz] [--version=X] [--arch=amd64]
#              [--node-binary=/path] [--skip-verify]
#
# 默认 --all，产出 out/pkg/ 下三种成品包（rpmbuild 缺失时 rpm 告警跳过）。
# 详见 README.md 与 docs/design/c4_deployment.md。

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
C4_ROOT="$(cd "$PACKAGING_DIR/.." && pwd)"
source "$PACKAGING_DIR/scripts/lib.sh"

# ── 参数解析 ───────────────────────────────────────────────
TARGET="all"
SKIP_VERIFY=0

# 环境覆盖（NODE_BINARY / NODE_DIR），--node-binary 优先级最高
NODE_BINARY_ENV="${NODE_BINARY:-}"
NODE_DIR_ENV="${NODE_DIR:-}"
NODE_BINARY_ARG=""

# 版本：默认读 VERSION 文件
PKG_VERSION="$(tr -d '[:space:]' < "$PACKAGING_DIR/VERSION")"

while [ $# -gt 0 ]; do
    case "$1" in
        --all)           TARGET="all"; shift ;;
        --rpm)           TARGET="rpm"; shift ;;
        --deb)           TARGET="deb"; shift ;;
        --targz)         TARGET="targz"; shift ;;
        --version=*)     PKG_VERSION="${1#*=}"; shift ;;
        --arch=*)        PKG_ARCH="${1#*=}"; shift ;;
        --node-binary=*) NODE_BINARY_ARG="${1#*=}"; shift ;;
        --skip-verify)   SKIP_VERIFY=1; shift ;;
        -h|--help)
            sed -n '2,8p' "$0"
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ── 架构映射（设计 §2.1：当前仅 amd64）─────────────────────
GOARCH="$PKG_ARCH"
case "$PKG_ARCH" in
    amd64)       RPM_ARCH="x86_64" ;;
    arm64)       RPM_ARCH="aarch64" ;;
    aarch64)     RPM_ARCH="aarch64" ;;
    *) die "unsupported arch: $PKG_ARCH (only amd64 supported per design §2.1)" ;;
esac

# ── Node 二进制解析 ─────────────────────────────────────────
if [ -n "$NODE_BINARY_ARG" ]; then
    NODE_BINARY="$NODE_BINARY_ARG"
elif [ -n "$NODE_DIR_ENV" ]; then
    NODE_BINARY="$NODE_DIR_ENV/bin/node"
elif [ -n "$NODE_BINARY_ENV" ]; then
    NODE_BINARY="$NODE_BINARY_ENV"
else
    NODE_BINARY="/usr/bin/node"
fi
NODE_REAL="$(readlink -f "$NODE_BINARY")"
[ -x "$NODE_REAL" ] || die "node binary not found: $NODE_BINARY"

# ── 路径 ────────────────────────────────────────────────────
OUT_DIR="$PACKAGING_DIR/out"
STAGING="$OUT_DIR/staging"
PKG_OUT="$OUT_DIR/pkg"
WORK="$OUT_DIR/.work"

# ── 步骤计数（8 个基础步骤 + 组装步骤 + 校验）───────────────
TOTAL=8
case "$TARGET" in
    all) TOTAL=$((TOTAL + 3)) ;;
    deb|rpm|targz) TOTAL=$((TOTAL + 1)) ;;
esac
if [ "$SKIP_VERIFY" -ne 1 ]; then
    TOTAL=$((TOTAL + 1))
fi
CUR=0

info "C4 打包 c4-$PKG_VERSION ($CODENAME) / arch=$PKG_ARCH / node=$NODE_REAL"

# ── 1. 清理 staging ────────────────────────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "清理 out/staging + out/pkg"
rm -rf "$STAGING" "$PKG_OUT"
mkdir -p "$STAGING" "$PKG_OUT" "$WORK"

# ── 2. 构建 6 个 Go 静态二进制 ──────────────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "构建 6 个 Go MCP 静态二进制 (GOARCH=$GOARCH)"
install -d "$STAGING/usr/local/bin"
for svc in $MCP_SERVICES; do
    (
        cd "$C4_ROOT/mcp/$svc"
        CGO_ENABLED=0 GOOS=linux GOARCH="$GOARCH" \
            go build -trimpath -ldflags "-s -w" -o "$svc" .
    )
    install -m 0555 "$C4_ROOT/mcp/$svc/$svc" "$STAGING/usr/local/bin/$svc"
done

# ── 3. 构建 Agent（npm ci + tsc）────────────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "构建 Agent (npm ci && npm run build)"
(
    cd "$C4_ROOT/agent"
    npm ci
    npm run build
    # tsc 不拷贝 .txt：补齐 dist 硬依赖路径（super_worker.ts 硬致命读取）
    mkdir -p dist/super_worker/prompts
    cp src/super_worker/prompts/system.txt dist/super_worker/prompts/system.txt
    # 仅保留生产依赖（设计 §4.2 预打包 node_modules）
    npm prune --omit=dev
)
install -d "$STAGING/usr/local/lib/c4/agent"
cp -a "$C4_ROOT/agent/dist" "$STAGING/usr/local/lib/c4/agent/dist"
cp -a "$C4_ROOT/agent/node_modules" "$STAGING/usr/local/lib/c4/agent/node_modules"
mkdir -p "$STAGING/usr/local/lib/c4/agent/src/super_worker/prompts"
cp "$C4_ROOT/agent/src/super_worker/prompts/system.txt" \
    "$STAGING/usr/local/lib/c4/agent/src/super_worker/prompts/system.txt"

# ── 4. 构建前端（vite）──────────────────────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "构建前端 (npm ci && npm run build)"
(
    cd "$C4_ROOT/agent/frontend"
    npm ci
    npm run build
)
cp -a "$C4_ROOT/agent/frontend/dist" "$STAGING/usr/local/lib/c4/frontend"

# ── 5. 捆绑 Node 运行时 ─────────────────────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "捆绑 Node 运行时"
install -d "$STAGING/usr/local/lib/c4/agent/node/bin"
install -m 0555 "$NODE_REAL" "$STAGING/usr/local/lib/c4/agent/node/bin/node"

# ── 6. 注册表 JSON + agent.env.example ──────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "暂存 MCP 注册表 + agent.env.example"
install -d "$STAGING/usr/local/etc/c4/mcp-registry"
install -m 0555 "$C4_ROOT/config/mcp-registry/"*.json "$STAGING/usr/local/etc/c4/mcp-registry/"
install -m 0644 "$PACKAGING_DIR/agent.env.example" "$STAGING/usr/local/etc/c4/agent.env.example"

# ── 7. 暂存 install.sh（tar.gz 安装器）──────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "暂存 install.sh"
install -d "$STAGING/usr/local/lib/c4/scripts"
install -m 0755 "$PACKAGING_DIR/scripts/install.sh" "$STAGING/usr/local/lib/c4/scripts/install.sh"

# ── 8. 暂存 systemd 单元 ────────────────────────────────────
CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "暂存 systemd 单元"
install -d "$STAGING/etc/systemd/system"
install -m 0644 "$PACKAGING_DIR/systemd/c4-agent.service" \
    "$STAGING/etc/systemd/system/c4-agent.service"

# ── 组装函数 ────────────────────────────────────────────────
assemble_deb() {
    local deb_out
    deb_out="$PKG_OUT/c4_${PKG_VERSION}_${PKG_ARCH}.deb"
    CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "组装 DEB: $(basename "$deb_out")"
    install -d "$STAGING/DEBIAN"
    sed "s/@VERSION@/$PKG_VERSION/g" "$PACKAGING_DIR/deb/control.template" \
        > "$STAGING/DEBIAN/control"
    install -m 0755 "$PACKAGING_DIR/scripts/postinst.sh" "$STAGING/DEBIAN/postinst"
    install -m 0755 "$PACKAGING_DIR/scripts/prerm.sh"    "$STAGING/DEBIAN/prerm"
    install -m 0755 "$PACKAGING_DIR/scripts/postrm.sh"   "$STAGING/DEBIAN/postrm"
    fakeroot dpkg-deb --build --root-owner-group "$STAGING" "$deb_out" >/dev/null
    rm -rf "$STAGING/DEBIAN"
}

assemble_targz() {
    local tgz_out
    tgz_out="$PKG_OUT/c4-${PKG_VERSION}.tar.gz"
    CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "组装 tar.gz: $(basename "$tgz_out")"
    fakeroot tar --owner=root --group=root --numeric-owner \
        -czf "$tgz_out" -C "$STAGING/usr/local" .
}

assemble_rpm() {
    local rpm_out rpmroot spec
    rpm_out="$PKG_OUT/c4-${PKG_VERSION}.${RPM_ARCH}.rpm"
    CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "组装 RPM: $(basename "$rpm_out")"
    if ! command -v rpmbuild >/dev/null 2>&1; then
        warn "rpmbuild not found — skipping rpm (build on RHEL to produce it)"
        return 0
    fi
    rpmroot="$WORK/rpm"
    rm -rf "$rpmroot"
    mkdir -p "$rpmroot"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
    tar -czf "$rpmroot/SOURCES/c4-${PKG_VERSION}.tar.gz" -C "$STAGING" .
    spec="$rpmroot/SPECS/c4.spec"
    sed "s/@VERSION@/$PKG_VERSION/g" "$PACKAGING_DIR/rpm/c4.spec.template" > "$spec"
    rpmbuild -bb --define "_topdir $rpmroot" "$spec"
    cp "$rpmroot/RPMS/${RPM_ARCH}/c4-${PKG_VERSION}-1.${RPM_ARCH}.rpm" "$rpm_out"
}

case "$TARGET" in
    deb)   assemble_deb ;;
    rpm)   assemble_rpm ;;
    targz) assemble_targz ;;
    all)
        assemble_deb
        assemble_targz
        assemble_rpm
        ;;
esac

# ── 校验 ────────────────────────────────────────────────────
if [ "$SKIP_VERIFY" -ne 1 ]; then
    CUR=$((CUR + 1)); step "$CUR" "$TOTAL" "运行 scripts/verify.sh"
    C4_BUILD_VERSION="$PKG_VERSION" "$PACKAGING_DIR/scripts/verify.sh"
fi

# ── 汇总 ────────────────────────────────────────────────────
info ""
info "==== C4 打包完成 (c4-$PKG_VERSION, $CODENAME) ===="
for f in "$PKG_OUT"/*; do
    [ -f "$f" ] || continue
    info "$(basename "$f")  $(stat -c '%s' "$f") bytes  sha256=$(sha256sum "$f" | cut -d' ' -f1)"
done
info "================================================="
