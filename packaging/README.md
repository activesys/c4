# C4 打包（packaging）

C4 部署包构建管线，产出三种可安装制品（对应 `docs/design/c4_deployment.md`）：

| 格式 | 文件名 | 目标系统 |
|------|--------|---------|
| DEB | `c4_<version>_amd64.deb` | Debian / Ubuntu |
| RPM | `c4-<version>.x86_64.rpm` | RHEL / CentOS / Rocky / AlmaLinux / openEuler |
| tar.gz | `c4-<version>.tar.gz` | 无包管理器的精简环境（兜底） |

## 用法

```bash
cd c4/packaging

./build.sh --all        # 默认：构建全部三种制品
./build.sh --deb        # 仅 DEB
./build.sh --rpm        # 仅 RPM（需要本机装有 rpmbuild）
./build.sh --targz      # 仅 tar.gz
./build.sh --all --skip-verify   # 构建但跳过结构校验

./scripts/verify.sh     # 独立运行结构校验（out/staging + out/pkg）
```

产物输出到 `out/pkg/`，中间产物（staging 树、临时 rpmbuild 树）在 `out/` 下。

## 可调参数（knobs）

| 参数 | 默认 | 说明 |
|------|------|------|
| `--version=X` | 读 `VERSION` 文件 | 产品版本号（产物命名） |
| `--arch=amd64` | `amd64` | 目标架构；当前仅支持 amd64（设计 §2.1） |
| `--node-binary=/path` | `/usr/bin/node` | 捆绑的 Node 二进制路径 |
| 环境变量 `NODE_BINARY` | — | 同上（命令行参数优先） |
| 环境变量 `NODE_DIR` | — | Node 安装目录，等价 `NODE_DIR/bin/node` |
| `--skip-verify` | 关 | 跳过构建后的结构校验 |

> 说明：`--arch` 影响 Go 交叉编译的 `GOARCH` 与 RPM 的 `BuildArch`（amd64→x86_64、
> arm64→aarch64），但设计 §2.1 明确当前仅支持 amd64，其余架构未经验证。

## 布局常量（单一事实来源）

安装布局常量集中在 `scripts/lib.sh`：`BIN_DIR` `/usr/local/bin`、
`LIB_DIR` `/usr/local/lib/c4`、`ETC_DIR` `/usr/local/etc/c4`、`ACCOUNT` `c4`、
`ACCOUNT_HOME` `/home/c4`、`PKG_VERSION`、`PKG_ARCH`、`RPM_ARCH`、`CODENAME`。
`build.sh` / `verify.sh` 直接 source 该文件；生命周期脚本（`postinst.sh` 等）
为在目标机独立运行而内联默认值（`c4` / `/home/c4`），需与 `lib.sh` 保持一致。
`systemd/c4-agent.service` 中的 `User=` / `--config-dir` 亦需同步。

## 安装 / 删除

```bash
# DEB
sudo dpkg -i c4_1.0.0_amd64.deb
# RPM
sudo rpm -ivh c4-1.0.0.x86_64.rpm
# tar.gz（等效 postinst）
sudo tar -xzf c4-1.0.0.tar.gz -C /usr/local
sudo /usr/local/lib/c4/scripts/install.sh
```

安装（postinst / install.sh，幂等）自动完成：创建 `c4` 账户 → 生成
`/home/c4/.local/c4/{state,logs}`（0700）→ 应用 `/usr/local/etc/c4` 属主
`root:c4` 并从 `agent.env.example` 创建 `agent.env`（640，仅当不存在时）→
安装 systemd 单元并 `daemon-reload`。**不自动 start、不自动 enable**。

启动（首次需在 `/home/c4/.local/c4/agent.json` 填入配置后）：

```bash
sudo systemctl enable --now c4-agent
journalctl -u c4-agent -f
```

删除：

```bash
sudo systemctl stop c4-agent && sudo systemctl disable c4-agent
sudo dpkg -r c4        # 或 rpm -e c4     —— 默认保留 agent.env 与运行时数据
sudo dpkg --purge c4   # 彻底清除：删除 c4 账户与 /home/c4（含 agent.env）
```

## 注意事项 / 已知限制

- **glibc ≥ 2.28**（由捆绑的 Node.js 22 LTS 决定；Go 二进制已静态编译，无此约束）。
  对应 RHEL/CentOS/Rocky/AlmaLinux 8、Ubuntu 20.04、Debian 10、openEuler 20.03。
  不支持 CentOS 7 / RHEL 7（glibc 2.17）等更老系统。
- **依赖打包机已有 Node**：当前捆绑的是**打包机自身的** `/usr/bin/node`（默认）。
  需保证打包机 Node 为 22 LTS，且其 glibc/libstdc++ 下限与目标一致（见 §10 待定项）。
- **rpmbuild 缺失即跳过 RPM**：本机若无 `rpmbuild`（rpm-build 包），`--all` 会
  打印警告并继续（退出码仍为 0）；请在 RHEL 构建主机上执行以产出 RPM。
- **npm 依赖联网**：`npm ci` 与 `go build` 需要网络（或已预热的 npm / Go 模块缓存）。
- **包内不含 `agent.json`**：Agent 权威配置为站点私有数据，由管理员在目标机一次性
  生成（设计 §5.2 / §7.1），随包仅分发 `agent.env.example` 模板。

## 目录结构

```
packaging/
├── build.sh                  # 编排入口
├── VERSION                   # 产品版本号
├── agent.env.example         # 环境变量模板
├── systemd/c4-agent.service  # systemd 单元
├── scripts/
│   ├── lib.sh                # 布局常量 + 辅助函数（被 build.sh / verify.sh source）
│   ├── install.sh            # tar.gz 安装器（独立版 postinst，内嵌 systemd 单元）
│   ├── postinst.sh           # deb postinst / rpm %post 逻辑
│   ├── prerm.sh              # deb prerm / rpm %preun（stop + disable）
│   ├── postrm.sh             # deb postrm / rpm %postun（仅 purge 删账户）
│   └── verify.sh             # 结构校验 V1–V8
├── deb/control.template      # DEBIAN/control（@VERSION@ 占位）
└── rpm/c4.spec.template      # RPM spec（@VERSION@ 占位）
```

## §10 待定项（对应设计 §10）

- **捆绑 Node 的官方获取方式**：当前直接捆绑打包机 `/usr/bin/node`。正式发布应改为
  随包携带**官方 LTS 预编译包**（与目标架构匹配），由 `build.sh` 下载并校验
  SHA256 后 staging，避免依赖打包机环境；如需支持 CentOS 7（glibc 2.17）还需评估
  musl 静态 Node 或目标机源码编译。
- 精确 CPU / 内存 / 磁盘上限（性能基线测试后回填）。
- 目标 OS 正式清单与最低 glibc/内核版本。
- 架构扩展（aarch64 等）的构建与验证。
- 安装包签名（GPG / RPM 签名）与校验。
- winston 文件日志轮转、state filesystem 持久化（当前内存态）。
- 运行期注册的 MCP 注册表分层（当前仅随包内置 5 个注册表 JSON）。
