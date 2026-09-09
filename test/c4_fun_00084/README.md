# C4_FUN_00084 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00084（Agent 管理点位持续显示与终止控制）
> **对应需求**：C4_RS_00056
> **设计参考**：`docs/design/agent.md` §3.6.3（会话模型）/ §3.6.5（REST 契约）/ §3.6.6（重启语义）、`docs/design/web.md` §3.5

会话生命周期全部经 REST 确定性驱动：`POST /api/display`（创建）、`GET /api/display`
（轮询）、`POST /api/display/stop`（终止）。自然语言订阅话术为 LLM 呈现层（❌），不在
本方案正测范围。会话后端 timer 独立于前端轮询——测试暂停轮询不影响会话推进。

---

## 1. 被测对象与前置条件

测试栈自建（硬约束与栈契约同 `c4_fun_00082/README.md` §1：tmp config-dir、独立
instance `c4_ft84`、服务端口 19xxx 避让、无 systemd、无 sudo；**点位 fixture 同
`c4_fun_00082/README.md` §4**——wt1.windspeed / wt1.power / wt1.oiltemp）。
shm 播种为测试线程受控周期 seqlock 直写。c4_shm_manager 由 Agent 栈自启。

---

## 2. 测试用例

### test_session_create.py（TC1~TC5b：创建与多点）

| TC | 场景 | 操作 | 预期 |
|----|------|------|------|
| TC1 | 最简创建 | `POST {pointKeys:["wt1.windspeed"]}` | 200；响应为**会话载荷（与 GET 同形）**：`active=true`、`sessionId` 非空、`mode=realtime`（缺省）、`intervalMs=1000`（缺省） |
| TC2 | 多点订阅 | pointKeys=[A,B,C] | points **集合**与请求一致（3 条；顺序无契约） |
| TC3 | 累积模式 | `mode="cumulative"` | 响应与轮询均标 cumulative；轮询带 `?since=<tick>` 返回条目且每条携带自身 tick |
| TC4 | 非法 key | pointKeys 含不存在的 key | **非 2xx**、`active` 不变、错误体为文本错误信息（REST 错误体契约未定义，断言最小面），会话不建立 |
| TC5 | intervalMs 越下限 | intervalMs=100 | **拒绝**（非 2xx，不钳制——agent.md §3.6.3），会话不建立 |
| TC5b | 点数超限 | pointKeys > 1000 | 拒绝订阅，提示分批（§3.6.3 points 上限） |

### test_session_modes.py（TC6~TC9：两种模式与游标）

| TC | 场景 | 操作 | 预期 |
|----|------|------|------|
| TC6 | 实时值模式推进 | 写线程周期 500ms，按 intervalMs 轮询 3 次 | 每次轮询 value/timestampMs 反映最新写入（后一次 ≥ 前一次） |
| TC7 | 累积模式累积（intervalMs=250 钉死） | cumulative + **会话建立前预写初值 w0** + 写线程每 300ms 变值，运行 5s | 累积记录 **20±2** 条（= ticks）；每条含 t/v/tick，tick 单调递增；**值序列为已写值**（每条等于某一已写入值、对应写入序号单调非降；允许跳值，不允许错值） |
| TC8 | 累积游标增量 | `?since=<tick>` 拉取两次 | 第二次只返回自游标后的新增条目；游标推进无重复 |
| TC9 | 游标跨会话 | 记录 sessionId₁ 游标 → stop → 新建会话 sessionId₂ → 旧游标拉取 | sessionId 已变化 → 按契约返回**新会话全量**（安全默认，agent.md §3.6.5）；**不得返回跨会话混合数据** |

### test_session_terminate.py（TC10~TC16：五类终止）

| TC | 场景 | 操作 | 预期 |
|----|------|------|------|
| TC10 | 时长终止 | durationMinutes≈0.1（6s），intervalMs=1000 | ~6s 后 `active=false`；`lastSession.endedReason=completed_duration`，`finalTick` ∈ [4, 8] |
| TC11 | 次数终止 | refreshCount=5（intervalMs=1000） | 恰 5 tick 后结束；`endedReason=completed_count`；`finalTick=5` |
| TC12 | 主动停止 | 运行中 `POST /api/display/stop`（无参） | 立即 `active=false`；`endedReason=stopped` |
| TC13 | 切换即取消 | 会话 A 运行中再 POST 创建会话 B | B 为唯一活跃会话；`lastSession` 为 A（`endedReason=replaced`） |
| TC14 | 点级停止 | 双点会话 `stop {pointKeys:[A]}` | A 消失、B 仍在、会话继续；再 stop B → 会话结束 |
| TC15 | 降级不终止 | kill c4_shm_manager（轮询截止 10s 等 `degraded=true`） | 会话仍 active（degraded=true），**不触发任何终止**（C4_RS_00056 终止途径封闭） |
| TC16 | 终止后摘要冻结 | TC10 结束后持续轮询 3 次 | `finalTick` 不再变化；`lastSession` 保留至下一会话建立 |

### test_session_degraded.py（TC17~TC18：降级与重启）

| TC | 场景 | 操作 | 预期 |
|------|------|------|------|
| TC17 | 调用失败降级 | kill c4_shm_manager（轮询截止 10s 等 degraded=true） | `degraded=true`；各点 value/state 保持最后成功值（不杜撰）；恢复路径 ❌ 待 C4_FUN_00021 |
| TC18 | Agent 重启（测试进程内） | AgentHandle.restart()（**非 systemctl**） | `active=false` 且**无** lastSession（会话仅内存）；shm 持久：重启前后 `/dev/shm/{instance}` 的 write_seq 相同（不归零），测试直写仍可继续 |

---

## 3. 断言面说明

- **所有状态转换断言使用轮询+截止时间**（poll-until-deadline），禁止固定 sleep 后单次断言；
- 时间容差统一 ±2 tick（覆盖 intervalMs 粒度与调度抖动）；
- `endedReason` 枚举封闭：`completed_count` / `completed_duration` / `stopped` /
  `replaced` / `error`——任何路径不得产生枚举外原因；
- 会话结束后 `/api/display` 必须返回 `{active:false, lastSession:{...}}`（除 TC18 重启场景）；
- LLM 层（❌ 不正测）：NL 订阅/停止话术、`display_points`/`stop_display` 的自然语言触发路径
  （REST 为同一服务入口的确定性等价面）。
