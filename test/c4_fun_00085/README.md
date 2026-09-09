# C4_FUN_00085 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00085（Agent 支持点位发现与批量选择）
> **对应需求**：C4_RS_00057
> **设计参考**：`docs/design/agent.md` §3.6.4（`list_points` 工具，writer-only 枚举）、§3.6.5（`GET /api/points`）

点位发现的确定性面是 `GET /api/points`（读 `~/.local/c4/config.json` 语义的 **writer 类服务**
points，reader 引用不产生独立条目）。歧义消解的对话交互（列出候选供选择）与 `list_points`
等控制面 LLM 工具的自然语言触发为 LLM 层（❌ LLM 推理），不在本方案正测范围——本方案
验证 REST 返回的候选数据足以支撑该交互。

---

## 1. 被测对象与前置条件

测试栈自建（硬约束与栈契约同 `c4_fun_00082/README.md` §1：tmp config-dir、独立
instance `c4_ft85`、服务端口 19xxx 避让、无 systemd、无 sudo）。config fixture 见 §2。

---

## 2. 测试 fixture 与用例（test_discovery.py，TC1~TC8）

fixture（writer 两设备含**跨设备重名** windspeed；reader 引用 windspeed/voltage——
分别支撑歧义与去重两类用例；另含空 points 设备）：

```json
{"c4_shm_manager": {"writer": ["c4_asfp2_server"], "reader": ["c4_asfp2_client"]},
 "c4_asfp2_server": [
   {"id": "wt1", "port": 19001,
    "points": [{"id": "windspeed", "addr": 3000}, {"id": "power", "addr": 3001},
               {"id": "oiltemp", "addr": 3002}]},
   {"id": "pv1", "port": 19002,
    "points": [{"id": "windspeed", "addr": 3100}, {"id": "voltage", "addr": 3101}]},
   {"id": "emptydev", "port": 19003, "points": []}],
 "c4_asfp2_client": [
   {"id": "center", "ip": "127.0.0.1", "port": 19900,
    "points": [{"key": "wt1.windspeed", "addr": 5000},
               {"key": "pv1.voltage", "addr": 5001}]}]}
```

| TC | 场景 | 操作 | 预期 |
|----|------|------|------|
| TC1 | 全量枚举 + writer-only 去重 | `GET /api/points` | **恰 5 条**（writer 3+2+0）；每条含 key / addr / shm_id / 所属实例；reader 引用的 windspeed/voltage **不产生重复条目**；emptydev 贡献 0 条 |
| TC2 | key 组成 | 检查 wt1.windspeed 条目 | key=`wt1.windspeed`（`{实例}.{点名}`），addr=3000，shm_id 为正整数 |
| TC3 | 按设备筛选 | `?filter=wt1` → 3 条；`?filter=pv1` → 2 条 | 仅对应设备点 |
| TC4 | 按关键词筛选（歧义候选） | `?filter=windspeed` | **恰 2 条**（wt1 与 pv1 各一）——跨设备重名完整呈现，供歧义消解列候选 |
| TC5 | 无匹配 | `?filter=nothing` | 空列表（200，非错误） |
| TC6 | 批量选择数据支撑 | TC1 结果按实例分组 | wt1 组 3 点可整体作为一次订阅的 pointKeys（与 00084 TC2 衔接） |
| TC7 | shm_id 与 shm 实际一致 | 预写探针值后，TC1 返回的 shm_id 经 `read_points` 读取 | 值与预写一致（key→shm_id 映射正确性） |
| TC8 | 空匹配空态（无会话/无点） | `?filter=emptydev` | 空列表（200） |

---

## 3. 断言面说明

- **只断言 REST 输出**的集合与字段；候选呈现话术与 `list_points` 控制面工具为 LLM 层（❌）；
- 去重规则（writer-only）是本功能的核心断言：config 中 reader 段与 writer 段同 key
  同时存在是常态（见 fixture），枚举重复即 TC1 失败；
- TC7 需 `c4_shm_manager read_points` 工具可用（C4_FUN_00082 测试方案 §2）；
- **所有断言使用轮询+截止时间**（poll-until-deadline），禁止固定 sleep 后单次断言。
