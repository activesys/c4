# func_case_e2e — E2E 测试设计与环境模型

> **被测对象**：`test/func_test_case.md` 用例 16~28（含用例 10）的端到端 runner（`run_cases.py`）
> **设计参考**：`docs/design/c4_architecture.md` §3.1.1（部署形态：独立系统服务与 Unix Socket 通道）、
> §3.1.2（变更事务与 Agent 启动/恢复四级瀑布）、§3.1.1 故障矩阵；`docs/design/c4_deployment.md` §6.3（shm 损坏恢复）

本目录的 runner 以**隔离 agent 实例**驱动 func_test_case.md 的用例：独立 config-dir、
独立 shm（`c4_e2e`）、19xxx 端口映射（9001→19001、9002→19002、9900→19900、9901→19901），
不与生产 agent 及用户 Web 测试互相干扰。用法（root）：

```bash
python3 run_cases.py <case>   # case: prereq|16|17|18|19|20|21|22|23|24|25|26|27|28|29|10|all
```

---

## 1. 环境模型（架构变更后）

Agent 与每个 MCP 服务是**相互独立的 systemd 服务**（方案 A：安装的全部 MCP 单元默认启用、
常驻运行；未收到 start 指令前零数据路径实例）：

- Agent 是 **MCP 客户端**，经 Unix domain socket 连接各服务（`/run/c4/<service>.sock`，
  权限 0660 属主 `c4:c4`，绑定前 unlink）——**从不拉起 MCP 进程**；
- 进程生命周期归 systemd；数据路径**实例**生命周期归 Agent 经 MCP 工具管理
  （start/stop 作用于实例，进程不随 stop 退出）；
- 测试环境（本目录）无 systemd，等价自建栈：测试自行启动 `c4_shm_manager` 与 Agent
  （独立 config-dir，socket 指向 tmp 目录），二者为平级进程、无父子关系——与
  `c4/test/c4_fun_00082/README.md` §1 的栈契约一致；
- MCP 存活状态 = 连接状态推导（仅用于 Web 展示/告警）；新增 MCP 服务为部署期操作，
  Agent 重启是识别边界。

## 2. 用例与前置链

| 用例 | 前置 | 验证要点（详见 func_test_case.md 各用例「关键点」） |
|------|------|--------------------------------------------------|
| prereq / 10 | — | 用例 1 隔离复刻（接入 hnals_wt1），为 16~28 建立前置态 |
| 16~22 | prereq | 增量加点 / 删点 / 冲突拒绝 / 新风机并存（Stop-Start 后端口重监听、既有 shm_id 不变） |
| 23~24 | prereq | 端口/点表冲突的逐项识别与可读拒绝 |
| 25 / 26 | 21 | 整机删除 / 删至 0 台（合法空态：config 段空数组 = 期望零实例，start 幂等 success） |
| 27~28 | prereq | 删除不存在的风机 / 批量模糊删除逐台确认 |
| **29（新增，见 §3）** | prereq | **Agent 崩溃恢复瀑布**——数据不中断、收敛后配置变更能力恢复 |

## 3. 用例 29（新增设计）：Agent 运行中 kill -9 的崩溃恢复 E2E

> **已实现**——`run_cases.py` 的 `case: 29`（前置自行执行 prereq；写_seq 经
> c4_shm_manager 的 `read_points` MCP 调用观测，kill -9 后注入驱动采集验证递增）。设计依据
> c4_architecture.md §3.1.1 故障矩阵第 1 行与 §3.1.2 四级瀑布。

### 3.1 前置态

`prereq` 完成（hnals_wt1 在线：接收 19001、转发 → 19900，数据流持续）。

### 3.2 步骤与断言

| # | 操作 | 断言 |
|---|------|------|
| 1 | 记录 shm 各点 `write_seq` 基线；`kill -9` Agent 进程（模拟崩溃） | Agent 进程退出；**数据接入不中断**——19001 持续监听、→19900 转发持续，等待 ≥2 个采集周期后各点 `write_seq` 均已递增（MCP 进程常驻、实例照常运行——Agent 不在实时数据路径中，C4_RS_00030/00031） |
| 2 | Agent 崩溃期间发起一次配置变更（对点追加，经 REST/Web） | Agent 不可达（连接拒绝）——崩溃窗口内无配置变更能力（预期行为，不作缺陷） |
| 3 | 重新启动 Agent（等价 systemd 拉起），轮询 `GET /api/services` 至 200 | 四级瀑布执行：L0 config.json 健康（无 pending_change.json 标记）→ L1 连接全部服务 socket（c4_shm_manager 硬前置）→ L2 收敛：对各数据服务 start，返回 **ALREADY_RUNNING（一等成功路径，无动作）**→ L3 监控接续 |
| 4 | 收敛完成后发起删除类配置变更（用例 25 句式：「删除2号风机」，若 2# 在线；否则用例 26 句式：「删除1号风机」走删至 0 台） | 变更正常受理、按钮确认后**执行落地**：config.json 双实例成对删除、端口释放、shm 块回收（用例 25/26 的关键点断言全部适用）——证明瀑布收敛后配置变更能力完整恢复 |
| 5 | 数据流终态 | 存留实例数据流持续（删除不波及存留风机）；若删至 0 台，则系统回到「等待首次接入」空态，重新执行 prereq 应可完整接入 |

### 3.3 实现要点（留给 runner 增补）

- kill -9 用 `SIGKILL` 直接杀 Agent 进程（测试栈内无 systemd，重启由测试执行；
  生产中由 `c4-agent` 单元 `Restart=always` 等价完成）；
- 步骤 4 是本用例的核心区分点：收敛失败/半收敛时删除类变更会被闸门拦截或挂起——
  以「删除能落地」证明瀑布真正收敛（L2 信任 start 契约返回，不做独立状态探测）；
- 所有状态转换断言使用轮询+截止时间（poll-until-deadline），禁止固定 sleep 后单次断言；
- 若崩溃恰落在变更事务中（`pending_change.json` 存在）属另一用例族，见
  `c4/test/agent/README.md` §3.2.3.4 / §3.2.4.2（恢复 .prev.1 + 完整 Stop-Start +
  报告「接入不成功」），本用例不覆盖。
