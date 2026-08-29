# C4 部署设计

> **版本**：v0.1.0 | **最后更新**：2026-08-28 | **父文档**：[c4_architecture.md](c4_architecture.md)
> **对应功能**：[C4_FUN_00081](../specification/c4_function.md), [C4_FUN_00035](../specification/c4_function.md), [C4_FUN_00064](../specification/c4_function.md)
> **对应需求**：[C4_RS_00254](../specification/c4_requirement.md), [C4_RS_00220](../specification/c4_requirement.md), [C4_RS_00221](../specification/c4_requirement.md), [C4_RS_00222](../specification/c4_requirement.md), [C4_RS_00015](../specification/c4_requirement.md)

---

## 1. 概述

C4 由三类运行时组件组成：

| 组件 | 语言 | 运行方式 | 交付形态 |
|------|------|---------|---------|
| MCP 服务集群（6 个） | Go | 静态二进制 | 独立可执行文件 |
| Agent | Node.js / TypeScript | `node dist/index.js` | `dist/` + `node_modules/` |
| Web 前端 | React / TypeScript | 纯静态文件（由 Agent 托管） | 构建产物 `dist/` |

本文定义 C4 的打包要求、部署方式、安装/删除/升级操作，以及部署后的软件目录布局。目标是让场站人员无需专业 Linux 知识即可完成安装与运维（对应 C4_RS_00254）。

### 1.1 打包设计原则

部署包遵循以下三条硬性原则：

1. **架构**：目标 OS 为 Linux；现场硬件不限于 amd64（可能 aarch64、arm64 等），**当前仅支持 amd64**，打包脚本以 `GOARCH` 参数化以便未来扩展。
2. **依赖最小化**：对外界依赖降到最低——Go 服务静态编译（消除 glibc 门槛）、Agent 预打包 `node_modules`（目标机无需联网安装）、Node.js 运行时随包捆绑（不依赖目标机已有 Node）。
3. **依赖锁定 LTS**：现场 OS 多为老旧版本，必要的运行时依赖必须锁定长期支持（LTS）版本，不使用目标机已过时或缺失的系统组件。

---

## 2. 部署前置依赖

### 2.1 操作系统

| 项 | 要求 | 说明 |
|----|------|------|
| 内核 | Linux | 依赖 POSIX 共享内存，无 Windows / macOS 支持 |
| 架构 | amd64（x86-64） | 二进制为 `ELF 64-bit`。Linux 亦可能为 aarch64、arm64、loongarch64 等其他架构，**当前仅支持 amd64** |
| glibc | 见下 | 取决于二进制链接方式 |

**glibc 约束（关键）**：

- 当前构建产物**动态链接 glibc**（`file` 显示 `dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2`），最高符号版本为 **GLIBC_2.34**（在 Ubuntu 22.04 / glibc 2.35 上编译）。
- 因此动态版本要求目标系统 **glibc ≥ 2.34**：Ubuntu 22.04+、Debian 12+、RHEL/Rocky/AlmaLinux 9+、openEuler 22.03+。**CentOS 7 / RHEL 7/8 / Debian 11 / Ubuntu 20.04 无法运行**。
- **打包时必须消除该约束**：6 个 MCP 服务均为纯 Go 库、无 CGO 依赖（虽引用 `modelcontextprotocol/go-sdk`、`golang.org/x/sys` 等第三方库，但均为纯 Go 实现），用 `CGO_ENABLED=0 go build` 生成**完全静态二进制**，即可在任意 glibc 版本运行。这是打包流程的第一步（见 §4.1）。

**最低支持版本结论**：

| 组件 | 最低要求 | 说明 |
|------|---------|------|
| Go MCP 服务（静态编译后） | Linux 内核 ≥ 3.2（Go 1.25+） | 无 glibc 依赖 |
| 捆绑的 Node.js 22 LTS | **glibc ≥ 2.28** ＋ **libstdc++ ≥ 6.0.25**（`GLIBCXX_3.4.25`） | Node 18+ 官方预编译二进制在 RHEL 8 构建；libstdc++ 随 GCC 8.1+ 提供，裁剪过的极简系统可能缺失 |

> **整体最低下限 = glibc 2.28**（由捆绑的 Node.js 决定，Go 静态二进制不构成约束）。
> 对应发行版：**RHEL / CentOS / Rocky / AlmaLinux 8、Ubuntu 20.04、Debian 10、openEuler 20.03**。
> ❌ 不支持：**CentOS 7 / RHEL 7（glibc 2.17）、Ubuntu 18.04（glibc 2.27）、Debian 9（glibc 2.24）**。
> 若需支持 CentOS 7 等更老系统，须改用 musl 静态 Node 构建（实验性）或在目标机从源码编译 Node，详见 §10。

**Node.js 版本与 glibc 下限对应关系**：

| Node 版本 | 构建平台 | glibc 下限 | 状态 |
|-----------|---------|-----------|------|
| **22 LTS（当前捆绑，开发机 v22.23.1）** | RHEL 8 | **2.28** | 维护期至 2027-04 |
| 20 LTS | RHEL 8 | 2.28 | 已 EOL（2026-04） |
| 18 LTS | RHEL 8 | 2.28 | 已 EOL（2025-04） |
| 16 | CentOS 7 | 2.17 | 已 EOL（2023-09），不采用 |

> 注意：回退到 20/18 LTS **不会降低 glibc 门槛**——Node 18 起全部在 RHEL 8 构建，下限均为 glibc 2.28；只有已 EOL 的 Node 16 才是 glibc 2.17。

### 2.2 运行时软件依赖

| 依赖 | 版本要求 | 用途 | 是否随包捆绑 |
|------|---------|------|-------------|
| Node.js | 锁定 **22 LTS**（开发机 v22.23.1；回退 20/18 LTS 亦为 glibc 2.28，不降低门槛） | 运行 Agent | **随包捆绑**（不依赖目标机已有 Node） |
| `node_modules`（langchain / express 等） | 见 `agent/package.json` | Agent 运行时依赖 | 随包捆绑（预打包） |
| `/dev/shm`（tmpfs） | 已挂载 | POSIX 共享内存 | 系统默认 |
| `ipcs` / `ipcrm`（util-linux） | 任意 | 运维排障（非运行时必需） | 系统工具 |

### 2.3 硬件要求

| 项 | 建议值 | 说明 |
|----|--------|------|
| 共享内存 | 默认 10 万点 × 32B ≈ 3.2 MB | 有配置时按点数 ×2 扩容，实际占用见 c4_architecture.md §3.3 |
| CPU / 内存 | 待实测（C4_RS_00203） | Agent 侧以 Node 进程为基准；MCP 服务轻量 |
| 磁盘 | 程序 ~50 MB + 日志（journald） | 采集数据不落盘（C4_RS_00131） |

> 精确的 CPU/内存/磁盘上限在性能基线测试后回填（对应 C4_RS_00203「详细设计中定义资源消耗上限」）。

---

## 3. 软件组成与交付物

### 3.1 组件清单

| 组件 | 源码目录 | 构建产物 | 部署目标 |
|------|---------|---------|---------|
| c4_shm_manager | `mcp/c4_shm_manager/` | `c4_shm_manager` | `/usr/local/bin/` |
| c4_modbus_client | `mcp/c4_modbus_client/` | `c4_modbus_client` | `/usr/local/bin/` |
| c4_iec104_client | `mcp/c4_iec104_client/` | `c4_iec104_client` | `/usr/local/bin/` |
| c4_asfp2_client | `mcp/c4_asfp2_client/` | `c4_asfp2_client` | `/usr/local/bin/` |
| c4_asfp2_server | `mcp/c4_asfp2_server/` | `c4_asfp2_server` | `/usr/local/bin/` |
| c4_influxdb_client | `mcp/c4_influxdb_client/` | `c4_influxdb_client` | `/usr/local/bin/` |
| c4-agent | `agent/` | `dist/` + `node_modules/` | `/usr/local/lib/c4/` |
| Node.js LTS 运行时 | 官方预编译包 | `bin/node` | `/usr/local/lib/c4/agent/node/` |
| Web 前端 | `agent/frontend/` | `dist/`（静态） | `/usr/local/lib/c4/frontend/` |
| MCP 注册表 | `config/mcp-registry/` | `*.json` | `/usr/local/etc/c4/mcp-registry/` |
| 环境变量文件 | 手动生成 | `agent.env` | `/usr/local/etc/c4/agent.env` |

### 3.2 部署包格式

| 格式 | 目标系统 | 说明 |
|------|---------|------|
| **RPM** | RHEL / CentOS / Rocky / AlmaLinux / openEuler | 工业现场主流 |
| **DEB** | Debian / Ubuntu | 备选 |
| **自包含 tar.gz** | 无包管理器的精简环境 | 兜底，配合安装脚本 |

每种格式均包含相同的组件清单与安装脚本（§6），仅包管理器适配层不同。

### 3.3 版本号与命名

C4 采用标准语义化版本 **x.y.z** 作为版本号，另以「化学元素周期表」作为**开发代号**（codename）。

**版本号（x.y.z）**：

| 段 | 含义 |
|----|------|
| x（主版本） | 不兼容的重大变更 |
| y（次版本） | 向后兼容的功能新增 |
| z（补丁） | bug 修复 / 性能优化 |

**开发代号（`C4` + `{元素符号}` + `{序号}`）**：

- 元素符号按**原子序数递增顺序**取用，主版本 x 对应第 x 个元素；**C（碳）元素跳过不用**。
- 序号为该主版本内的发布序号，从 1 起递增，主版本推进时重置为 1。
- 开发代号仅用于对外宣传/沟通，**不参与包名与版本比较**。

**元素顺序**（按原子序数，跳过 C）：

```
H(1) → He(2) → Li(3) → Be(4) → B(5) → ~~C(6)~~ → N(7) → O(8) → F(9) → Ne(10) → Na(11) → Mg(12) → Al(13) → Si(14) → P(15) → S(16) → Cl(17) → Ar(18) → K(19) → Ca(20) → …（共 118 种元素，跳过 C 后可用 117 个主版本代号）
```

**版本 ↔ 代号对照示例**：

| 版本号 | 开发代号 |
|--------|---------|
| 1.0.0 | C4H1 |
| 1.1.0 | C4H2 |
| 2.0.0 | C4He1 |

**安装包命名**（用 x.y.z 版本号）：

| 格式 | 命名规则 | 示例 |
|------|---------|------|
| RPM | `c4-{版本}.{arch}.rpm` | `c4-1.0.0.x86_64.rpm` |
| DEB | `c4_{版本}_{arch}.deb` | `c4_1.0.0_amd64.deb` |
| tar.gz | `c4-{版本}.tar.gz` | `c4-1.0.0.tar.gz` |

> 说明：C4 版本号为**产品发布版本**，独立于内部组件版本（`agent/package.json` 的 `1.0.0`、Go 模块的 git tag 等）及各文档版本头（如 `v0.4.13`）。

---

## 4. 打包要求

### 4.1 Go MCP 服务二进制

构建命令（每个服务）：

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o <name> .
```

要求：

- **静态链接**：`CGO_ENABLED=0`，产物 `ldd` 应显示 `not a dynamic executable`。
- **strip**：`-ldflags "-s -w"` 去除符号表与调试信息（当前约 8 MB，strip 后约 5 MB）。
- **架构**：当前仅构建 `GOARCH=amd64`；Linux 亦可能是 aarch64 等其他架构。打包脚本应将 `GOARCH` 作为参数化变量，未来按需扩展（Go 交叉编译无需目标机环境，纯 Go 库 + `CGO_ENABLED=0` 可安全交叉编译）。
- 产物不带 `debug_info`（当前 `/usr/local/bin/c4_*` 未 strip，需在打包时修正）。

### 4.2 Agent

```bash
cd agent && npm ci && npm run build   # tsc → dist/
```

要求：

- `dist/` 由 TypeScript 编译生成，不含 `src/` 与 `node_modules/` 之外的 devDependencies。
- **预打包 `node_modules`**（`npm ci --omit=dev`），避免目标机联网安装依赖（工业现场通常无外网）。
- 部署包内**捆绑指定版本的 Node.js LTS 运行时**（当前 22 LTS），不依赖目标机已有 Node（现场 OS 老旧，系统 Node 常缺失或过时）。

### 4.3 Web 前端

```bash
cd agent/frontend && npm ci && npm run build   # tsc --noEmit && vite build → dist/
```

- 交付**构建后的静态 `dist/`**，目标机无需 Node.js 即可被 Agent 的 Express 托管（见 §5.2 目录布局）。
- 前端构建产物体积应远小于 `node_modules`，`node_modules` 不随包交付。

### 4.4 打包产物校验清单

- [ ] 6 个二进制均为静态链接（`ldd` 为空）
- [ ] `file` 显示 `statically linked` / 无 `debug_info`
- [ ] `agent/dist/index.js` 可 `node dist/index.js --help` 正常解析
- [ ] 前端 `dist/index.html` 存在且引用资源路径正确
- [ ] `mcp-registry/*.json` 中 `binary_path` 指向 `/usr/local/bin/` 实际安装路径

---

## 5. 部署方式

### 5.1 目录布局（安装后）

采用标准 Unix 目录约定，严格区分「只读程序」「系统配置」「可写运行时数据」：

```
/usr/local/
├── bin/
│   ├── c4_shm_manager
│   ├── c4_modbus_client
│   ├── c4_iec104_client
│   ├── c4_asfp2_client
│   ├── c4_asfp2_server
│   └── c4_influxdb_client          # 6 个 MCP 二进制（root 所有，0555）
├── lib/c4/
│   ├── agent/                       # Agent 运行时（捆绑 node 运行时 + dist/ + node_modules/）
│   │   ├── node/                    # 捆绑的 Node.js LTS 运行时（含 bin/node，OS 无需预装 Node）
│   │   │   └── bin/node
│   │   ├── dist/index.js
│   │   └── node_modules/
│   └── frontend/                    # Web 静态资源（vite build 产物）
└── etc/c4/
    ├── agent.env                    # 敏感环境变量（DEEPSEEK_API_KEY），root:c4 0640
    └── mcp-registry/                # MCP 注册表 JSON（只读，5 个数据路径服务）
        ├── c4_modbus_client.json
        ├── c4_iec104_client.json
        ├── c4_asfp2_client.json
        ├── c4_asfp2_server.json
        └── c4_influxdb_client.json
        # 注：c4_shm_manager 不在此注册，由 agent.json 的 shm_manager.binary 直接指定

~/.local/c4/                         # 运行时数据（c4 专用账户可写）
├── agent.json                       # Agent 权威配置（启动必读）
├── config.json                      # MCP 全量配置（数据路径权威数据源）
├── config.json.bak                  # config.json 备份（原子写入前的快照）
├── abbr_registry.json               # 场站缩写记忆库（可重建派生数据）
├── state/                           # 状态（当前内存态，重启重建；filesystem 持久化待实现，见 §10）
└── logs/                            # 日志目录（当前 console 经 journald；winston 文件日志待实现，见 §10）

/dev/shm/{instance_id}               # POSIX 共享内存（tmpfs，进程退出后回收）
```

**权限约定**（对应 C4_RS_00015 最小权限）：

| 路径 | 属主 | 权限 | 说明 |
|------|------|------|------|
| `/usr/local/bin/c4_*` | root:root | 0555 | 只读可执行 |
| `/usr/local/lib/c4/` | root:root | 0555 | 只读 |
| `/usr/local/etc/c4/` | root:c4 | 0555 | 只读 |
| `~/.local/c4/` | c4:c4 | 0700 | 仅 c4 账户可写 |

> **账户名约定**：上表及全文中的 `c4` 仅为**示例账户名**，并非硬性要求。代码不校验账户名（`agent/src/index.ts` 无 `getuid`/用户检查，仅用 `node:os` 的 `homedir()` 展开 `~`），因此**任意专用非 root 账户**均可运行，运行时目录自动落到该账户自己的 `$HOME/.local/c4/`。只需保证 systemd 的 `User=`、`--config-dir` 与 `agent.json` 内路径三者指向同一账户。

> **注册表位置说明**：`mcp-registry/` 是随包分发的只读静态数据（描述本版本内置 MCP 服务的能力、`config_schema`、`binary_path`），不属于实例私有运行时数据，故置于系统级只读目录 `/usr/local/etc/c4/mcp-registry/`（`root:c4 0555`），而非 `~/.local/c4`。未来实现「运行期注册新 MCP 服务」（C4_FUN_00079）时，将分层为系统只读层 + 用户可写层（`~/.local/c4/mcp-registry/`），Agent 合并加载，见 §10。

### 5.2 裸机部署（默认）

1. 安装系统级程序文件（§5.1 中 `/usr/local/` 部分）。
2. 创建专用非 root 账户（示例名 `c4`，任意名称均可，C4_RS_00015）。
3. 由管理员以 root 一次性生成 `~/.local/c4/agent.json`（或首次启动向导 C4_FUN_00080 引导生成）。
4. 通过 systemd 以 `c4` 账户启动 Agent（守护进程，C4_RS_00014）。

### 5.3 容器部署（可选）

C4 可用容器交付（对应 C4_RS_00222「容器、裸机」），但需满足：

- **共享内存**：挂载 `/dev/shm`（或 `--shm-size`），MCP 服务之间依赖 POSIX 共享内存。
- **最小权限**：容器内以非 root 用户运行（C4_RS_00015）。
- **网络**：MCP 服务对外端口（ASFP2 / Modbus / IEC104 / InfluxDB）需按配置映射。

---

## 6. 安装 / 删除 / 升级操作

### 6.1 安装

**RPM/DEB 方式**：

```bash
# RPM
sudo rpm -ivh c4-<version>.x86_64.rpm
# DEB
sudo dpkg -i c4_<version>_amd64.deb
```

包管理器 `%post` / `postinst` 脚本自动完成：创建 `c4` 账户 → 生成目录骨架 → 安装 systemd 单元 →（可选）引导首次配置。

**自包含 tar.gz 方式**：

```bash
sudo tar -xzf c4-<version>.tar.gz -C /usr/local
sudo /usr/local/lib/c4/scripts/install.sh   # 等效 postinst
```

### 6.2 删除

```bash
sudo systemctl stop c4-agent && sudo systemctl disable c4-agent
sudo rpm -e c4        # 或 dpkg -r c4
```

删除行为约定：

- 卸载程序文件与系统配置（`/usr/local/{bin,lib,etc}/c4/`）。
- `agent.env` 含敏感密钥：**默认保留**，仅 `--purge` 时删除。
- **保留** `~/.local/c4/` 运行时数据（agent.json / config.json / abbr_registry.json），供重装后恢复（用户可选 `--purge` 一并删除）。
- 共享内存 `/dev/shm/{instance_id}` 由进程退出自动回收，卸载时无需处理。

### 6.3 升级

| 升级对象 | 策略 | 依据 |
|---------|------|------|
| MCP 服务二进制 | 停止→替换→重启（按服务逐个滚动） | C4_RS_00251（滚动升级） |
| Agent | 停旧启新，不影响已运行 MCP 管道 | C4_RS_00252、C4_FUN_00022 |
| Web 前端 | 覆盖静态文件即可，无需重启数据管道 | — |

升级脚本流程（`%post` / `upgrade.sh`）：

1. 备份 `~/.local/c4/agent.json` 与 `config.json`。
2. 替换 `/usr/local/` 程序文件。
3. `systemctl restart c4-agent`（Agent 启动时执行无条件 Stop-Start 恢复，见 agent.md §3.2.3）。
4. 校验 `GET /api/services` 返回 200，服务进程恢复。

### 6.4 服务管理（systemd）

```ini
[Unit]
Description=C4 Agent
After=network.target

[Service]
User=c4
Group=c4
# 注意：--config-dir 必须与 User= 的 home 一致（/home/<账户>/.local/c4），换账户名需同步修改
ExecStart=/usr/local/lib/c4/agent/node/bin/node /usr/local/lib/c4/agent/dist/index.js --config-dir /home/c4/.local/c4
Restart=on-failure
RestartSec=5
# 环境变量：DEEPSEEK_API_KEY 经 EnvironmentFile 注入
EnvironmentFile=/usr/local/etc/c4/agent.env
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/home/c4/.local/c4 /dev/shm
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
```

**常用运维命令**：

```bash
# 启动
sudo systemctl start c4-agent

# 设置开机自启
sudo systemctl enable c4-agent

# 停止
sudo systemctl stop c4-agent

# 重启
sudo systemctl restart c4-agent

# 查看状态
systemctl status c4-agent

# 实时查看日志
journalctl -u c4-agent -f

# 查看最近 200 条日志
journalctl -u c4-agent -n 200
```

**进程模型**：

- systemd 只管理 **Agent**（`c4-agent`）一个单元。
- Agent 通过 stdio 按需 spawn MCP 服务子进程（c4_shm_manager 及各数据路径服务），其生命周期由 Agent 内部管理（启动/停止/重启，见 agent.md §3.2.3）。
- MCP 服务**无独立 systemd 单元**；运维人员只操作 `c4-agent`，不应手动启停单个 MCP 服务。
- 停止 Agent（`systemctl stop`）时，Agent 的 SIGTERM 处理会关闭 MCP manager，MCP 子进程随之一并终止。

---

## 7. 配置与数据文件

### 7.1 配置文件清单

| 文件 | 位置 | 属主 | 说明 |
|------|------|------|------|
| `agent.json` | `~/.local/c4/agent.json` | c4 | Agent 权威配置，启动必读，缺失则 FATAL 退出 |
| `mcp-registry/*.json` | `/usr/local/etc/c4/mcp-registry/` | root:c4 | MCP 服务注册信息（`binary_path` 指向 `/usr/local/bin/`） |
| `agent.env` | `/usr/local/etc/c4/agent.env` | root:c4 | `DEEPSEEK_API_KEY` 等敏感环境变量，`chmod 640` |

`agent.json` 结构（Zod schema，见 agent/src/index.ts）：

```jsonc
{
  "instance_id": "c4",
  "model": { "provider": "deepseek", "name": "deepseek-chat",
             "temperature": 0, "max_tokens": 4096, "api_key_env": "DEEPSEEK_API_KEY" },
  "server": { "host": "0.0.0.0", "port": 9988, "cors_origin": "*" },
  "mcp_registry": { "path": "/usr/local/etc/c4/mcp-registry" },
  "shm_manager": { "binary": "/usr/local/bin/c4_shm_manager",
                   "config_path": "~/.local/c4/config.json" },
  "state": { "backend": "memory", "path": "~/.local/c4/state" },   // 字段已定义但未使用（状态现为内存态，见 §10）
  "logging": { "level": "info", "dir": "~/.local/c4/logs" },       // dir 已定义但未落盘（现为 console/journald，见 §10）
  "frontend": { "dir": "/usr/local/lib/c4/frontend" },
  "site": { "name": "华能阿拉善", "abbr": "hnals" }   // 可选，场站单例
}
```

### 7.2 数据与运行文件

| 文件 | 生命周期 | 说明 |
|------|---------|------|
| `config.json` | 首次接入创建，跨重启永久 | MCP 全量配置，数据路径权威数据源（agent.md §3.2） |
| `config.json.bak` | 随 config.json 更新 | 原子写入前的快照，损坏时恢复 |
| `abbr_registry.json` | 可重建派生数据 | 场站缩写记忆库；丢失/损坏可从 config.json 重建 |
| `state/` | 运行期 | 当前内存态（AgentStateTracker，重启重建）；filesystem 持久化待实现（见 §10） |
| `logs/` | 运行期 | 当前 console 日志经 journald 收集；winston 文件日志/轮转待实现（见 §10） |
| `/dev/shm/{instance_id}` | 进程生命周期 | POSIX 共享内存，进程退出回收 |

---

## 8. 安全与最小权限

- **专用非 root 账户**：Agent 与 MCP 服务以专用非 root 账户运行（示例名 `c4`，任意名称均可，C4_RS_00015、C4_FUN_00064）。
- **最小权限**：MCP 服务仅拥有执行其数据接入任务所需权限（C4_RS_00120）。
- **敏感信息隔离**：`DEEPSEEK_API_KEY` 存于 `agent.env`（`chmod 640`），不写入 `agent.json`。
- **目录权限**：程序目录只读（0555），运行时数据仅运行账户可写（0700），见 §5.1 权限表。
- **NoNewPrivileges / 无特权提升**：systemd 单元禁用额外权限（C4_RS_00004 不替代安全基础设施）。

---

## 9. 部署验证清单

安装完成后逐项核对：

| 检查项 | 命令 / 方法 | 期望 |
|--------|------------|------|
| 二进制可执行且静态 | `ldd /usr/local/bin/c4_shm_manager` | `not a dynamic executable` |
| 二进制版本 | `c4_shm_manager --version` | 输出版本号（若支持） |
| Node 可运行 Agent | `node /usr/local/lib/c4/agent/dist/index.js --help` | 不报语法/模块错误 |
| 前端资源可访问 | `curl http://127.0.0.1:9988/` | 返回 `index.html` |
| Agent 就绪 | `curl http://127.0.0.1:9988/api/services` | HTTP 200 |
| 共享内存 | `ls /dev/shm/` | 出现 `{instance_id}` 文件 |
| 服务自启 | `systemctl enable c4-agent && reboot` | 重启后 Agent 自动恢复 |

---

## 10. 待定项（待性能基线/选型确认后回填）

- 精确 CPU / 内存 / 磁盘上限（C4_RS_00203）。
- 目标操作系统正式清单与最低 glibc/内核版本（C4_RS_00220）。当前技术下限为 **glibc 2.28**（由捆绑的 Node.js 决定，而非 Go 静态二进制）；如需支持 CentOS 7（glibc 2.17）需评估 musl 静态 Node 或源码编译。
- 架构扩展：当前仅 amd64，未来是否支持 aarch64 等架构（取决于目标现场硬件，需额外构建对应 `GOARCH` 二进制并验证）。
- 随包捆绑的 Node.js LTS 具体版本与获取方式（需提供与目标架构匹配的 Node 构建，例如官方 LTS 预编译包或静态二进制分发）。
- 安装包签名（GPG/RPM 签名）与校验，配合许可密钥机制（C4_RS_00305）。
- **winston 文件日志 + 轮转**：当前 Agent 仅 console 日志（经 journald 收集），`logging.dir` 字段已定义但未落盘；需实现 winston 文件传输与轮转，或明确放弃 `logs/` 目录。
- **state filesystem 持久化**：当前状态为内存态（AgentStateTracker，重启重建），`state.backend`/`state.path` 字段已定义但未使用；需实现 filesystem 持久化，或明确改为内存态。
- **运行期注册的注册表分层**（C4_FUN_00079 落地时）：当前仅随包内置服务，注册表单目录 `/usr/local/etc/c4/mcp-registry/`（只读）；实现运行期注册后需分层（系统只读 + 用户可写 `~/.local/c4/mcp-registry/`），并将 `mcp_registry.path` 改为多目录加载（`paths[]`）。
