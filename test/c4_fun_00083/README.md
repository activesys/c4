# C4_FUN_00083 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00083（Agent 展示点位刷新频率，支撑与厂家原生系统对点）
> **对应需求**：C4_RS_00055
> **设计参考**：`docs/design/agent.md` §3.6.3（频率统计）、§3.6.2（自适应阈值，含窗口剪空后保留语义）

频率 = 会话 tick 间 `read_points` 返回 `seq` 的变位差分（滑动 60s 窗口）。
展示口径：近 60s 刷新次数 + 平均间隔。

---

## 1. 被测对象与前置条件

测试栈自建（硬约束与栈契约同 `c4_fun_00082/README.md` §1：tmp config-dir、独立
instance `c4_ft83`、非冲突端口、无 systemd、无 sudo、轮询就绪、teardown 杀进程 +
shm_unlink）。shm 播种为测试线程受控周期 seqlock 直写。

---

## 2. 测试用例（test_freq_stats.py，TC1~TC8）

| TC | 场景 | 预置与操作 | 预期 |
|----|------|-----------|------|
| TC1 | 固定周期统计 | 点 A 写线程周期 500ms（±10ms 抖动），建会话 intervalMs=250，运行 10s | `freq.count` ∈ [18, 21]（±10ms 抖动下最多 21 次写入：⌊10s/490ms⌋+1），`freq.intervalMs` ∈ [450, 560] |
| TC2 | 静止点（短会话） | 点 B 预写 ts=now 后全程不写；会话 ≤55s | `freq.count=0`；state 仍 `ok`（未超阈值）——**频率 0 与 stale 标注解耦** |
| TC3 | 多点独立统计 | A 周期 500ms、B 周期 2s，同会话运行 10s | 两点 freq 各自独立：A ∈ [18, 21]、B ∈ [3, 6]（折算规则 ⌊10s/2s⌋+1），互不串扰 |
| TC4 | 滑动窗口边界（slow，~140s） | A 每 500ms 写 70s 后停止；会话 terminate=manual 存活 | 随旧变位滑出 60s 窗口，`freq.count` 在 3 个观察点**单调非增**至 0 |
| TC5 | 首 tick 不计变位（slow，≥60s） | 会话建立时点已有 seq=N（预置），此后不变 | 首个 tick 仅初始化 lastSeq；**完整 60s 窗口内** `freq.count=0`（不把 0→N 记为变位） |
| TC6 | 频率随实际周期呈现 | A 周期 3s，会话 intervalMs=250，运行 15s | `\|freq.intervalMs − 3000\| ≤ 300`；`freq.count` ∈ [3, 6]（折算规则 ⌊15s/3s⌋+1） |
| TC7 | 降级期不污染统计 | 会话运行中 kill c4_shm_manager（轮询截止 10s 等 degraded=true），持续 ≥3 轮 | `degraded=true` 期间 `freq.count` 与 `tick` 均不推进 |
| TC8 | 自适应阈值（slow，~140s） | 点 C 周期 25s 写入 ≥55s（≥2 变位，观测平均 ≈25s → 有效阈值 75s，**窗口剪空后保留**） | 静默 65s（<75s）时 state 仍 `ok`；**轮询至 `stale`（截止静默 90s）**（>75s 后转 `stale`） |

> TC4/TC5/TC8 为长时用例，`pytest.mark.slow` 标注。TC7 的恢复路径（shm_manager
> 重生后自动续读）依赖 Agent MCP 故障自愈（C4_FUN_00021，❌ 未实现），自愈落地后
> 补充恢复断言（与 `c4_fun_00084` TC17 注一致）。

---

## 3. 断言面与容差说明

- **只断言 REST 输出**（`freq.count` / `freq.intervalMs`），不断言内部 changes 数组；
- 容差来源：tick 粒度（intervalMs）与写线程调度抖动——interval 容差 ±10%；count 边界按折算
  规则（上限 +1 / 下限 −3）；
- 窗长折算规则：窗口未满 60s 时，预期上限 = ⌊窗长/最短周期⌋ + 1（写入抖动可多一次写入），
  下限 = 上限 − 3（与 TC1 的 [18, 21] 一致）；
- **所有状态转换断言使用轮询+截止时间**（poll-until-deadline），禁止固定 sleep 后单次断言。
