# C4_FUN_00047 测试方案

> **对应功能**：`c4/docs/specification/c4_function.md` C4_FUN_00047
> **对应需求**：C4_RS_00095
> **设计参考**：`c4/docs/design/c4_asfp2_client.md` §4

C4_FUN_00047：C4 发送 ASFP2 数据 — 从共享内存 shm_id 读取数据，按最新版本编码为 ASFP2 数据包，发送到中心侧或其他 C4 实例。仅处理数值类型，自动检测属性开关（SAME_DATA_TYPE / KEY_SEQUENCE / SAME_TIMESTAMP）优化带宽。

---

## 1. 测试目标

验证 `c4_asfp2_client` 在 `start` 成功后：

1. 从共享内存读取已订阅 shm_id 的数据
2. 仅发送数值类型，非数值类型被过滤
3. 按 KEY_SEQUENCE 连续性拆分子组
4. 每个子组独立检测 SAME_DATA_TYPE 和 SAME_TIMESTAMP
5. 按 ASFP2 最新版本（ASFPV211）编码数据包
6. smart=1 时 timestamp 毫秒归零
7. BIT 压缩模式（三属性全开且 type=BOOLEAN/BIT）
8. TCP 断连后自动重连

---

## 2. 测试架构

### 2.1 数据流路径

```
 asfp2_client (CLI)       c4_asfp2_server (注入端)       c4_asfp2_client (SUT)       asfp2_server (验证端)
 ────────────────         ──────────────────────       ────────────────────        ───────────────────────
 发送 ASFP2 数据     →    接收 → 解析 → 写入 SHM   →    读取 SHM → 编码 → 发送  →    接收 → stdout 输出
 (port A)                 (port A, writer)               (reader, → port B)            (port B, --t1 0 --t2 0)
                                                                                      │                  │
                                                                              tcpdump 抓包          stdout 解析
                                                                              Flag/Count/Attr      key/ts/value
```

- **数据注入**：`asfp2_client` CLI 发送 ASFP2 数据包到 `c4_asfp2_server`（port A）
- **SHM 中转**：`c4_asfp2_server` 将接收到的数据按 `addr → shm_id` 写入 SHM
- **SUT**：`c4_asfp2_client` 从 SHM 读取 → ASFPV211 编码 → 发送到 port B

**双路验证**：

| 验证路径 | 工具 | 验证内容 |
|---------|------|---------|
| **包结构** | `tcpdump -w` → Python pcap 解析 | Flag 版本、Count（包内条目数）、Attribute flags（KEY_SEQUENCE / SAME_DATA_TYPE / SAME_TIMESTAMP） |
| **数据值** | `asfp2_server --t1 0 --t2 0` stdout | 每条数据的 key、timestamp、value 是否与注入值匹配 |

### 2.2 测试二进制

| 二进制 | 用途 | 关键参数 |
|--------|------|---------|
| `c4_shm_manager` | 创建 SHM，分配 shm_id | — |
| `c4_asfp2_server` | 注入端 — 收 `asfp2_client` 数据 → 写 SHM | — |
| `c4_asfp2_client` | **SUT** — 读 SHM → 编码发送 | — |
| `asfp2_client` | 数据注入 — 发 ASFP2 到 port A | `--type`, `--ts-start`, `-B/-E` |
| `asfp2_server` | 验证端 — 监听 port B，输出接收数据 | `--t1 0 --t2 0` 关闭心跳，避免自生成 `>ASFP200>` 输出 |
| `tcpdump` | 抓 port B 流量 | `-i lo -w /tmp/cap.pcap port 9800` |

Python 仅负责进程编排、tcpdump 启停、pcap 解析和 stdout 解析。**不实现** ASFP2 协议编码/解码。

### 2.3 测试时序

```
1. c4_shm_manager → create_shm → adjust_shm → 关闭
2. c4_asfp2_server (port A) → start → 就绪
3. c4_asfp2_client (SUT) → start → 连接 port B
4. asfp2_server --t1 0 --t2 0 -p 9800 > /tmp/rx.txt 2>&1 & → 启动验证端
5. tcpdump -i lo -w /tmp/cap.pcap port 9800 & → 开始抓包
6. asfp2_client CLI → 发送数据到 port A
7. wait (2×timer + 余量)
8. 停止 tcpdump → Python pcap 解析 → 断言包结构
9. 停止 asfp2_server → Python 解析 /tmp/rx.txt → 断言数据值
10. cleanup
```

### 2.4 ASFP2 Header 格式（v2.1.1）

```
Offset  Size  Field
0       8     Flag: "ASFPV211" (ASCII)
8       4     Length (高 2B = Attribute 高 2B, 低 2B = Length 低 2B)，网络序
12      2     Count: 数据项个数，网络序
14      2     Attribute 低 2B，网络序
────────────────
16      总 Header 长度
```

**Attribute 完整值**：`(length_and_attr >> 16) | attribute_low`

| 位 | 宏定义 | 值 |
|----|--------|----|
| bit 0 | KEY_SEQUENCE | 0x01 |
| bit 1 | SAME_DATA_TYPE | 0x02 |
| bit 2 | SAME_TIMESTAMP | 0x04 |

### 2.5 asfp2_server 输出格式

```text
Connection established, peer: 127.0.0.1:xxxxx
<ASFP211< key: 1000, timestamp: 1768848814000, data: 100
<ASFP211< key: 1001, timestamp: 1768848814000, data: 101
Connection Close, peer: 127.0.0.1:xxxxx
```

**解析规则**：
- 仅提取 `<ASFP211<` 行（前缀 `<` 表示接收到的数据）
- 正则：`<ASFP211<\s*key:\s*(\d+),\s*timestamp:\s*(\d+),\s*data:\s*(\d+)`
- `--t1 0 --t2 0` 关闭心跳后不会出现 `>ASFP211>` 行

---

## 3. 测试配置模板

### 3.1 标准配置（2 points）

```json
{
    "c4_shm_manager": {
        "writer": ["c4_asfp2_server"],
        "reader": ["c4_asfp2_client"]
    },
    "c4_asfp2_server": [
        {
            "name": "注入接收端", "id": "rx_inject",
            "port": 9700,
            "t1": 0, "t2": 0,
            "forward_kack": 255, "inverse_keep": 0,
            "points": [
                {"id": "p1", "addr": 1000, "shm_id": 0},
                {"id": "p2", "addr": 1001, "shm_id": 0}
            ]
        }
    ],
    "c4_asfp2_client": [
        {
            "name": "SUT 发送端",
            "ip": "127.0.0.1", "port": 9800,
            "t0": 30, "t1": 0, "t2": 0,
            "smart": 1, "forward_kack": 255, "inverse_keep": 0,
            "timer": 100,
            "points": [
                {"key": "rx_inject.p1", "addr": 1000, "shm_id": 0},
                {"key": "rx_inject.p2", "addr": 1001, "shm_id": 0}
            ]
        }
    ]
}
```

### 3.2 扩展配置

- 5 points（addr=1000~1004）：KEY_SEQUENCE 分组测试
- 不连续 key（addr=1000,1001,1005,1006）：拆分测试
- smart=0 配置：SAME_TIMESTAMP 不成立测试

### 3.3 期望的 shm_id 分配

writer_points=N，max_points=2N。addr 递增：1000 → shm_id=1，1001 → shm_id=2，…

---

## 4. 测试用例

### TC1: 基本发送 — UINT16，连续 key，同类型，同时间戳

- **前置**：标准配置（§3.1）。注入端和 SUT 均已 start
- **操作**：
  1. `asfp2_server --t1 0 --t2 0 -p 9800 > /tmp/rx.txt 2>&1 &`
  2. 启动 tcpdump 抓 port 9800
  3. `asfp2_client -s 127.0.0.1 -p 9700 -b 1000 -e 1001 -t 1 --type 4 -B 100 -E 200 --i0 10 --i1 10`
  4. 等待 400ms → 停止 tcpdump → 停止 asfp2_server
- **包结构预期**：
  - Flag = `"ASFPV211"`
  - Count = 2
  - Attribute = 0x07（三开关全开）
- **数据值预期**：
  - `/tmp/rx.txt` 含 key=1000 和 key=1001 两条记录
  - value 均在 [100, 200] 范围内

### TC2: KEY_SEQUENCE 连续 — 5 keys 一个包

- **前置**：5 points 配置（addr=1000~1004）
- **操作**：`asfp2_client -b 1000 -e 1004 --type 4` → 双路验证
- **包结构预期**：
  - 仅 **1 个 ASFP2 包**，Count = 5
  - KEY_SEQUENCE bit = 1
- **数据值预期**：5 条 key=1000~1004 记录，value 正确

### TC3: KEY_SEQUENCE 拆分 — 非连续 key 两个包

- **前置**：不连续 key 配置（addr=1000,1001,1005,1006）
- **操作**：分两次 `asfp2_client` 注入 4 个 key → 双路验证
- **包结构预期**：
  - **2 个 ASFP2 包**：包 A Count=2 (1000,1001)，包 B Count=2 (1005,1006)
  - 两包 KEY_SEQUENCE bit 均为 1
- **数据值预期**：4 条记录，全部正确

### TC4: SAME_DATA_TYPE — 全部同类型

- **前置**：标准配置。全部 type=4 (UINT16)
- **操作**：`asfp2_client --type 4` → 双路验证
- **包结构预期**：SAME_DATA_TYPE bit = 1
- **数据值预期**：2 条 UINT16 记录，value 正确

### TC5: SAME_DATA_TYPE 不成立 — 混合类型

- **前置**：标准配置。分两次 `asfp2_client` 注入：
  1. addr=1000: `--type 4` (UINT16)
  2. addr=1001: `--type 10` (FLOAT32)
  两次间隔 < 50ms（确保落在同一 SUT 轮询周期）
- **操作**：双路验证
- **包结构预期**：SAME_DATA_TYPE bit = 0
- **数据值预期**：key=1000(UINT16) + key=1001(FLOAT32) 均正确
- **容错**：若因时序原因未落同轮，会产生两个单点包（各自 SAME_DATA_TYPE=1）。此时可重复注入流程，最多重试 3 次。由于 SHM 写入（~µs）远小于 SUT 轮询周期（100ms），成功概率极高，但本测试接受底层竞态的非确定性限制

### TC6: SAME_TIMESTAMP (smart=1) — 同秒归零后相等

- **前置**：标准配置，smart=1。
- **操作**：`asfp2_client --ts-start 1000000 --type 4 -b 1000 -e 1001` → 双路验证
- **包结构预期**：SAME_TIMESTAMP bit = 1
- **数据值预期**：两条记录的 timestamp 均为 1000000（smart=1 归零），value 正确

### TC7: SAME_TIMESTAMP 不成立 (smart=0)

- **前置**：配置 smart=0。分次注入，时间戳差 ≥ 1000ms
- **操作**：双路验证
- **包结构预期**：SAME_TIMESTAMP bit = 0
- **数据值预期**：两条 timestamp 不同，value 正确

### TC8: 非数值类型过滤

- **前置**：3 points 配置（addr=1000~1002）
- **操作**：
  1. 注入 UINT16 到 addr=1000,1001
  2. 注入 STRING（`--type 12`）到 addr=1002 → `c4_asfp2_server` 丢弃此包
  3. 双路验证
- **包结构预期**：Count = 2（仅含数值类型）
- **数据值预期**：仅 key=1000,1001 出现，key=1002 不在输出中

### TC9: BIT 压缩模式

- **前置**：5 points 配置，注入 `--type 0 -z 5 -b 1000 -e 1004`（5 个 BOOLEAN）
- **操作**：双路验证
- **包结构预期**：
  - Count = 5
  - Attribute = 0x07（三开关全开）
  - Data 部分仅 ceil(5/8)=1 字节（非 5 字节）
- **数据值预期**：5 条 BOOLEAN 记录，value 为 0 或 1

### TC10: FLOAT32 编码

- **前置**：标准配置
- **操作**：`asfp2_client --type 10 -b 1000 -e 1001 -B 100 -E 200` → 双路验证
- **包结构预期**：Flag = `"ASFPV211"`
- **数据值预期**：FLOAT32 值正确（需用 `-P 8` 确保注入端也按网络序发送）

### TC11: TCP 断连重连 — 对端进程停止

- **前置**：标准配置。SUT 已连接 port 9800。配置 t0=5（缩短重连超时）
- **操作**：
  1. 首轮：启动 asfp2_server → 抓包 → 注入 → 验证正常
  2. 停止 asfp2_server → 停止抓包
  3. 新注入 → SUT 发送失败 → 启动 T0 重连
  4. 重启 asfp2_server → 重新抓包
  5. 等待 T0 + 余量 → 新注入 → 验证
- **包结构预期**：次轮 pcap 有有效 ASFPV211 包
- **数据值预期**：次轮 stdout 有正确数据记录

### TC12: TCP 断连重连 — KeepAlive 超时（T1/T2）

- **前置**：标准配置。SUT 配置 t1=2, t2=1, t0=5（短心跳周期）
- **操作**：
  1. 注入数据 → SUT 发送 → 验证正常
  2. **不注入新数据**，让 SUT 空闲（无数据发送）
  3. 等待 t1=2s → SUT 发送 KeepAlive → 启动 T2
  4. 对端 asfp2_server **不响应** KeepAlive（asfp2_server 未运行或防火墙规则丢弃 KeepAlive Ack）
  5. T2=1s 超时 → SUT 判定连接断开 → 启动 T0
  6. 启动 asfp2_server → 等待 T0=5s + 重连
  7. 注入新数据 → 验证发送恢复
- **包结构预期**：重连后 pcap 有有效 ASFPV211 包
- **数据值预期**：重连后 stdout 有正确数据记录
- **说明**：验证生产中更常见的故障模式——对端消失无 RST，仅通过 KeepAlive 超时检测

### TC13: smart=1 timestamp 零化验证

- **前置**：标准配置，smart=1。单 key（addr=1000）
- **操作**：`asfp2_client --ts-start 1768848814264 --type 4 -b 1000 -e 1000` → 双路验证
- **包结构预期**：—
- **数据值预期**：
  - stdout 中 timestamp = 1768848814000（毫秒置零）
  - 若 SAME_TIMESTAMP=1 → Mutable 中 timestamp 为 1768848814000
  - 若 SAME_TIMESTAMP=0 → Data item 中 timestamp 为 1768848814000

### TC14: 连续多轮 — 仅发送有新数据的 point

- **前置**：标准配置
- **操作**：
  1. 首轮：注入 → 抓包 + stdout → 验证正常
  2. **不注入新数据**，等待 2×timer+100ms
  3. 次轮：重新抓包 + stdout → 验证
- **包结构预期**：次轮 pcap **无 ASFP2 数据包**
- **数据值预期**：次轮 stdout **无新增数据记录**
- **说明**：SUT 的 `last_seen` 机制阻止重复发送

---

## 5. 实现注意

### 5.1 双路验证 helper

conftest.py 提供：

```python
# pcap 解析
parse_asfp2_packets(pcap_path, port) -> list[dict]
# → [{"flag": "ASFPV211", "count": 2, "attr": 0x07, ...}, ...]

# stdout 解析
parse_asfp2_server_output(txt_path) -> list[dict]
# → [{"key": 1000, "timestamp": 1768848814000, "value": 100}, ...]
```

### 5.2 asfp2_server 心跳禁用

**必须**传 `--t1 0 --t2 0`。否则验证端会自生成 `>ASFP211>` 行，污染输出。

### 5.3 tcpdump pcap 解析

- 过滤方向：仅 `src → port 9800`（SUT 发出的数据）
- **假设**：在 lo 回环 + timer=100ms 的发包间隔下，每个 TCP segment 包含恰好一个完整的 ASFP2 数据包。ASFP2 包远小于 lo MTU（65536），操作系统不会拆分
- 包边界定位：扫描 pcap payload，以 `'A'`（"ASFPV211" 首字节）为候选包头，按 Length 字段验证完整性
- **Length 合理性检查**：若 Length 与剩余 segment 字节数不匹配，触发解析失败（防止二进制数据中 `0x41` 导致的误同步）
- 若出现跨 segment 情况（极低概率），包会被跳过——解析器不缓冲跨段数据

### 5.4 等待策略

- SUT 轮询间隔 = `timer`(100ms)
- 注入完成后等待 `2×timer + 200ms`

### 5.5 端口固定值

| 端口 | 用途 |
|------|------|
| 9700 | `c4_asfp2_server` 注入端 |
| 9800 | `asfp2_server` 验证端 + tcpdump 抓包 |

### 5.6 禁止事项

- **不得用 Python 实现 ASFP2 协议编码/解码**
- **不得用 Python mmap 直接写共享内存**
- **不得调用 SUT 的 `status` 工具**

### 5.7 隔离性

- 每个 TC 使用独立的 `instance_id`、pcap 文件和 stdout 文件
- fixture teardown 清理所有进程、SHM 和临时文件
