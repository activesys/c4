# C4_FUN_00054 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00054
> **对应需求**：C4_RS_00096
> **设计参考**：`docs/design/c4_shm_manager.md` §1.2, §3.2

C4_FUN_00054 对应 `adjust_shm` 工具——根据配置文件计算所需点数，判断扩容与否，并
为所有点分配 shm_id。三个测试分支：

| 分支 | 行为 | 说明 |
|------|------|------|
| **不扩容** | `required_points ≤ max_points`，在空闲块中分配 | 无 ftruncate，remap_version 不变 |
| **扩容** | `required_points > max_points`，扩至 2 倍后分配 | ftruncate + remap_version++ |
| **错误路径** | 配置缺失 / 解析失败 / shm 未创建 / roots 失败 | 返回业务错误 |

---

## 1. 被测功能概述

### 1.1 adjust_shm 工具

| 属性 | 值 |
|------|-----|
| 工具名 | `adjust_shm` |
| 参数 | 无 |
| 前置 | Agent 已通过 Pause-Resume 暂停所有 MCP 进程（测试中不涉及） |
| 内部流程 | roots/list → 读配置 → 计算 required_points → 比较 current_max_points → 分配/扩容 → 回填配置 → 写回磁盘 |
| 成功返回 | `"success"` |

### 1.2 容量判断规则

```
if required_points ≤ current_max_points:
    → 不扩容：扫描 state=0 空闲块，为 shm_id=0 的新点分配
    → header.point_count = required_points
    → remap_version 不变
else:
    → new_max = required_points × 2
    → ftruncate(shm_fd, (new_max + 1) × 32)
    → header.max_points = new_max, header.remap_version++
    → munmap + mmap 新大小，新 block 写入 magic=0xC4DA7A00
    → 已有点保持原 shm_id，新点分配空闲 shm_id
    → header.point_count = required_points
```

### 1.3 测试关键：配置修改模式

测试流程为：

```
1. create_shm({instance_id}) → 初始 shm 创建 + 配置回填（shm_ids 已填入）
2. 修改配置文件 → 添加新采集点（新 point 的 shm_id=0）
3. adjust_shm() → 读配置，计算 required_points，分配/扩容
4. 验证 shm 状态 + 配置回填
```

---

## 2. 测试架构

```
c4/test/c4_fun_00054/
├── README.md                  # 本文件
├── conftest.py                # 复用 ../c4_fun_00053/conftest.py（或软链）
├── shm_helpers.py             # 复用 ../c4_fun_00053/shm_helpers.py（或软链）
└── test_adjust_shm.py         # 全部测试用例
```

### 2.1 被测对象 (SUT)

与 C4_FUN_00053 相同：Go 编译的 `c4_shm_manager` 二进制，通过 MCP stdio JSON-RPC 通信。

### 2.2 MCP 交互序列

```
1. initialize (握手，声明 roots 能力)
2. create_shm({instance_id}) → SUT 发起 roots/list → Python 应答
3. （Python 修改配置文件）
4. adjust_shm() → SUT 发起 roots/list → Python 应答 → SUT 执行分配 → 返回结果
5. 直接读取共享内存（`read_shm_header` / `read_shm_block`）→ 验证状态一致性
```

### 2.3 共享内存验证

Python 通过 `os.open("/dev/shm/c4_{id}", os.O_RDONLY)` + `mmap` 直接读取，
用 `struct.unpack` 按偏移解析各字段（同 C4_FUN_00053）。
**不使用 `query_status` MCP 工具**——所有验证均通过直接 shm 读取完成。

---

## 3. 配置辅助函数

### 3.1 初始配置工厂

```python
def _make_initial_config(writer_points, reader_points=None):
    """构造 create_shm 可用的初始配置，返回 dict。"""
    cfg = {
        "c4_shm_manager": {
            "writer": ["c4_modbus_client"],
            "reader": ["c4_asfp2_client"],
        },
        "c4_modbus_client": [{
            "id": "device1",
            "points": [
                {"id": f"p{i}", "uid": 1, "addr": 40000 + i, "shm_id": 0}
                for i in range(1, writer_points + 1)
            ],
        }],
        "c4_asfp2_client": [{
            "points": [] if reader_points is None else [
                {"key": f"device1.p{i}", "addr": 100 + i, "shm_id": 0}
                for i in range(1, reader_points + 1)
            ],
        }],
    }
    return cfg
```

### 3.2 配置修改：新增 Writer

```python
def add_writer_to_config(config_path, writer_name, service_id, point_defs):
    """在已有配置中添加新的 Writer section + 更新 writer 列表。"""
```

### 3.3 配置修改：追加 Point

```python
def add_points_to_config(config_path, service_id, new_points):
    """向已有 Writer 的 points 数组追加新点（shm_id=0）。"""
```

---

## 4. 测试用例

### 4.1 分支 A：不扩容（空闲块足够）

#### TC1: 不扩容 — 新增少量采集点

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 → max_points=4，point_count=2 |
| **操作** | 修改 config，追加 1 个新 point（total=3，≤ max_points=4）<br/>调用 `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | `max_points = 4`（不变），`remap_version = 0`（不变） |
| **验证 2** | 已有 point(p1) 保持 `shm_id=1`，已有 point(p2) 保持 `shm_id=2` |
| **验证 3** | 新 point(p3) 的 `shm_id=3`（从空闲块分配） |
| **验证 4** | 配置回填正确：all writer shm_ids > 0，reader shm_ids 匹配 |
| **验证 5** | Header 直接读取：`point_count = 3`，`max_points = 4`，`remap_version = 0` |
| **验证 6** | `read_shm_block(path, 3)`：新分配的 Block[3] `magic=0xC4DA7A00`, `state=0`, `type=0`；已有 Block[1]、Block[2] 的 magic、state 与 `adjust_shm` 前一致 |
| **验证 7** | shm 文件大小 = `(4 + 1) × 32 = 160` 字节（不变，未触发 ftruncate） |
| **清理** | `shm_unlink + 删除临时 config` |

#### TC2: 不扩容 — 无新增点（幂等性）

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 3 点 → max_points=6 |
| **操作** | 不修改 config，直接调用 `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | `max_points = 6`（不变），`remap_version = 0`（不变） |
| **验证 2** | 所有 shm_id 与 create_shm 后一致（p1→1, p2→2, p3→3） |
| **验证 3** | Header 直接读取：`point_count = 3`，`max_points = 6`，`remap_version = 0` |
| **验证 4** | shm 文件大小 = `(6 + 1) × 32 = 224` 字节（不变） |
| **说明** | `adjust_shm` 对无变化的配置是幂等的——不破坏已有分配 |

#### TC3: 不扩容 — 边界（required_points == max_points）

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 → max_points=4 |
| **操作** | 修改 config，追加 2 个新 point（total=4，== max_points=4）<br/>调用 `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | `max_points = 4`（不变），未触发扩容 |
| **验证 2** | 所有 4 点均有唯一 shm_id（1,2,3,4） |
| **验证 3** | Header 直接读取：`point_count = 4`，`max_points = 4`，`remap_version = 0` |
| **验证 4** | shm 文件大小 = `(4 + 1) × 32 = 160` 字节（不变） |
| **说明** | `==` 边界属于不扩容路径，不触发 ftruncate |

---

### 4.2 分支 B：扩容（超容量）

#### TC4: 扩容 — 单 Writer 新增点超容量

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 → max_points=4 |
| **操作** | 修改 config，追加 3 个新 point（total=5，> max_points=4）<br/>调用 `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | `max_points = 5 × 2 = 10` |
| **验证 2** | `remap_version` 递增（从 0 → 1） |
| **验证 3** | 已有 point(p1,p2) 保持 shm_id=1,2 |
| **验证 4** | 新点(p3,p4,p5) 分配到 shm_id=3,4,5 |
| **验证 5** | shm 文件大小 = `(10 + 1) × 32 = 352` 字节 |
| **验证 6** | Block[6..10] magic=0xC4DA7A00（扩容新增区域初始化正确） |
| **验证 7** | Header 直接读取：`point_count = 5`，`max_points = 10`，`remap_version = 1` |

#### TC5: 扩容 — 多 Writer 聚合超容量

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 → max_points=4 |
| **操作** | 修改 config：新增 `c4_iec104_client` Writer（4 点），total=6，> max_points=4<br/>调用 `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | `max_points = 6 × 2 = 12` |
| **验证 2** | modbus: p1→1, p2→2；iec104: p3→3, p4→4, p5→5, p6→6 |
| **验证 3** | `remap_version` 递增 |
| **验证 4** | Header 直接读取：`point_count = 6`，`max_points = 12`，`remap_version` > 0 |

#### TC6: 扩容 — 大幅增长

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 → max_points=4 |
| **操作** | 修改 config：追加 8 个新 point（total=10，> max_points=4）<br/>调用 `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | `max_points = 10 × 2 = 20` |
| **验证 2** | 新旧共 10 点均有连续 shm_id（1..10） |
| **验证 3** | shm 文件大小 = `(20 + 1) × 32 = 672` 字节 |
| **验证 4** | Block[1]（首块）、Block[10]（末个已分配块）、Block[20]（末块）magic 均 = 0xC4DA7A00 |

---

### 4.3 分支 C：配置解析错误

#### TC7: adjust_shm 缺少 c4_shm_manager 段

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm` 成功 |
| **操作** | 修改 config：删除顶层 `c4_shm_manager` key<br/>调用 `adjust_shm()` |
| **预期** | `isError: true`，`content[0].text` 以 `CONFIG_MISSING_SECTION` 开头 |
| **验证** | shm 状态不变（max_points, remap_version 与 create_shm 后一致） |

#### TC8: adjust_shm writer 为空

| 项目 | 内容 |
|------|------|
| **前置** | 配置文件存在（包含有效的 `c4_shm_manager.writer` 和 `reader`） |
| **操作** | 修改 config：`c4_shm_manager.writer = []`<br/>调用 `adjust_shm()` |
| **预期** | `CONFIG_MISSING_SECTION` |

#### TC9: adjust_shm reader 为空

| 项目 | 内容 |
|------|------|
| **前置** | 配置文件存在（包含有效的 `c4_shm_manager.writer` 和 `reader`） |
| **操作** | 修改 config：`c4_shm_manager.reader = []`<br/>调用 `adjust_shm()` |
| **预期** | `CONFIG_MISSING_SECTION` |

#### TC10: config JSON 格式错误

| 项目 | 内容 |
|------|------|
| **操作** | 修改 config 文件内容为 `{broken json`<br/>调用 `adjust_shm()` |
| **预期** | `isError: true`，错误码以 `CONFIG` 开头 |
| **验证** | shm 状态不变 |

#### TC11: 重复 key → DUPLICATE_KEY

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 |
| **操作** | 修改 config：追加一个与已有 point 相同 `id` 的 point（导致 `device1.p1` 重复）<br/>调用 `adjust_shm()` |
| **预期** | `DUPLICATE_KEY` 错误 |
| **验证** | shm 状态不变 |

#### TC12: Reader key 不存在 → UNKNOWN_READER_KEY

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 + asfp2 reader |
| **操作** | 修改 config：向 reader 追加一条 key=`"device1.ghost"`（Writer 中不存在）<br/>调用 `adjust_shm()` |
| **预期** | `UNKNOWN_READER_KEY` 错误 |

#### TC22: 空配置文件 `{}` → CONFIG_MISSING_SECTION

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm` 成功 |
| **操作** | 将 config 文件内容替换为 `{}`<br/>调用 `adjust_shm()` |
| **预期** | `isError: true`，`content[0].text` 以 `CONFIG_MISSING_SECTION` 开头 |
| **验证** | shm 状态不变 |
| **说明** | 与 TC7（删除 `c4_shm_manager` 段但保留其他 key）互补——验证完全空 JSON 同样触发错误 |

---

### 4.4 分支 D：基础设施错误

#### TC13: SHM 未创建 → SHM_NOT_CREATED

| 项目 | 内容 |
|------|------|
| **前置** | 无——不调用 `create_shm` |
| **操作** | 直接调用 `adjust_shm()`（config 文件存在） |
| **预期** | `isError: true`，`content[0].text` 以 `SHM_NOT_CREATED` 开头 |

#### TC14: roots/list 失败 → CONFIG_PATH_MISSING

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm` 成功 |
| **操作** | 调用 `adjust_shm()`，Python 对 roots/list 返回 MCP 错误 |
| **预期** | `isError: true`，`content[0].text` 以 `CONFIG_PATH_MISSING` 开头 |
| **验证** | shm 状态不变 |

---

### 4.5 分支 E：配置回填验证

#### TC15: 配置回填 — Writer shm_ids 全部填充

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 (max=4) |
| **操作** | 修改 config：追加 2 个新 point<br/>调用 `adjust_shm()` |
| **验证 1** | 重新读取 config 文件，所有 Writer point 的 `shm_id` 均 > 0 |
| **验证 2** | 新 point 的 `shm_id` 不在已有 point 的 shm_id 集合中（无重复） |
| **验证 3** | 没有 `shm_id = 0` 残留 |

#### TC16: 配置回填 — Reader shm_ids 匹配 Writer

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 + asfp2 reader 引用同 key |
| **操作** | 修改 config：追加 2 个新 point + reader 追加对应 key<br/>调用 `adjust_shm()` |
| **验证** | Reader point 的 `shm_id` = 对应 Writer point（相同 key）的 `shm_id` |

#### TC17: 配置回填 — 非 shm_id 字段保留

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点，reader 含 `valid`、`coeff`、`base` 字段 |
| **操作** | 修改 config：追加 1 个新 point<br/>调用 `adjust_shm()` |
| **验证 1** | 已有 point 的 `uid`、`addr`、`fun`、`type`、`swap` 字段值不变 |
| **验证 2** | Reader point 的 `valid`、`coeff`、`base`、`addr` 字段值不变 |
| **验证 3** | 仅有 `shm_id` 字段发生了写入变化 |

---

### 4.6 分支 F：状态一致性

#### TC18: 不扩容后 Header 状态一致性

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 (max=4, point_count=2) |
| **操作** | 追加 1 个新 point → `adjust_shm()` |
| **验证** | `read_shm_header` 返回：`magic=0xC4DA7A00`, `version=1`, `point_count=3`, `max_points=4`, `remap_version=0` |

#### TC19: 扩容后 Header 状态一致性

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 (max=4) |
| **操作** | 追加 3 个新 point（触发扩容）→ `adjust_shm()` |
| **验证** | `read_shm_header` 返回：`magic=0xC4DA7A00`, `version=1`, `point_count=5`, `max_points=10`, `remap_version=1` |

#### TC20: remap_version 行为

| 项目 | 内容 |
|------|------|
| **TC20a** | 不扩容 → `remap_version` **不变**（查询 Header 直接读取） |
| **TC20b** | 扩容 → `remap_version` **递增 1** |
| **说明** | `remap_version` 仅在 ftruncate 时递增，不扩容不触发 mmap 重映射，故不变 |

#### TC21: Block 内容完整性（扩容后）

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 → max=4 |
| **操作** | 追加 3 个新 point（total=5，> max=4，触发扩容）→ `adjust_shm()` |
| **验证 1** | 扩容不影响已有 Block 的内容：Block[1]、Block[2] 的 magic、state、reserved 与扩容前一致 |
| **验证 2** | 扩容后新增区域（超出原 max_points 的 block）magic=0xC4DA7A00，state=0 |
| **验证 3** | 新增 block 的 type、write_seq、timestamp、value 均为 0 |

#### TC23: 链式扩容（两次 `adjust_shm`，均触发扩容）

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 2 点 → max=4, point_count=2 |
| **操作 1** | 修改 config：追加 3 点（total=5，> max=4）→ `adjust_shm()` |
| **验证 1a** | `max_points = 10`，`remap_version = 1`，shm_ids 1..5，`point_count = 5` |
| **操作 2** | 修改 config：再追加 6 点（total=11，> max=10）→ `adjust_shm()` |
| **验证 2a** | 返回 `"success"` |
| **验证 2b** | `max_points = 11 × 2 = 22`，`remap_version = 2`（1→2），`point_count = 11` |
| **验证 2c** | 第一次的 5 个点保持 `shm_id=1..5`（跨两次扩容不变） |
| **验证 2d** | 新增 6 点分配到 `shm_id=6..11` |
| **验证 2e** | shm 文件大小 = `(22 + 1) × 32 = 736` 字节 |
| **验证 2f** | 第二次扩容新增区域 Block[11..22] magic=0xC4DA7A00，state=0 |

#### TC24: 默认 shm 上调用 `adjust_shm`（无配置创建 → 引入配置）

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm` 无配置文件 → max=100000，point_count=0 |
| **操作** | 创建配置文件（5 点），roots/list 指向新文件 → `adjust_shm()` |
| **验证 1** | 返回 `"success"` |
| **验证 2** | `max_points = 100000`（不变），`remap_version = 0`（不变） |
| **验证 3** | 5 点分配到 `shm_id=1..5` |
| **验证 4** | `point_count = 5`（从 0 更新） |
| **验证 5** | shm 文件大小 = `(100000 + 1) × 32 = 3,200,032` 字节（不变） |
| **验证 6** | 重新读取配置文件：所有 Writer point 的 `shm_id > 0`，无 `shm_id = 0` 残留 |
| **说明** | `required_points`(5) ≤ 100000，不扩容；测试从默认 100k 基线的不扩容路径，同时也是 `adjust_shm` 首次写回配置文件的路径（`create_shm` 时无配置） |

---

## 5. 实现注意

### 5.1 代码复用

`conftest.py` 和 `shm_helpers.py` 可直接复用 `c4_fun_00053/` 的版本。
修改点：
- `_find_binary()` 的候选路径可能需要调整（如 `c4_shm_manager` 路径变化）
- `isolated_shm` fixture 无需修改

### 5.2 配置修改时序

**关键**：`create_shm` 回填配置后，`adjust_shm` 必须以同一份配置文件作为输入。
Python 修改配置文件发生在 `create_shm` 和 `adjust_shm` 之间，需确保：

1. `create_shm` 完成后，读取回填后的配置文件
2. 在内存中修改（添加新 point），写回同一路径
3. 调用 `adjust_shm`，roots/list 返回同一路径

### 5.3 point_count 语义

 `point_count` 记录 Header 中已分配的 point 总数（`create_shm` 时置 `writer_points`，
 `adjust_shm` 时置 `required_points`）。

 **与 `state` 的关系**（按架构文档 `c4_architecture.md` §2.2.2）：
 - `state` 由 **Writer 进程首次写入时**置 1，不由 `c4_shm_manager` 的分配操作修改
 - 分配完成（`adjust_shm` 返回）时，新 block 的 `state` 为 0，但 `point_count` 已更新
 - `point_count` 在分配与 Writer 首次写入之间**可能暂高于** `state=1` 的 block 数
 - `point_count` 在所有 Writer 激活后与 `state=1` 的 block 数一致

 测试中 `state` 预期为 0（没有 Writer 运行），`point_count` 预期等于 `required_points`
 （反映分配状态，与 `state` 独立）。

### 5.4 字节序

与 C4_FUN_00053 相同：所有多字节字段存储为大端（网络字节序），Python struct 使用 `>` 前缀。

### 5.5 共享内存清理

- `isolated_shm` fixture 在 teardown 中调用 `shm_unlink`，确保无论测试成败都释放共享内存
- setup 中先尝试预防性清理（防止前次崩溃残留 shm 导致 O_EXCL 失败）

### 5.6 测试独立性

 每条测试用例各自创建/销毁共享内存和临时配置文件。用例之间无依赖，可任意顺序或并行执行。

### 5.7 已知覆盖盲区：`SHM_SYSCALL_FAILED`

 `ftruncate` / `mmap` 的 OS 级错误（ENOSPC、EINVAL 等）是设计 §5 明确列出的错误码，且
 在生产环境中是真实失效模式（`/dev/shm` 为 tmpfs，耗尽时 ftruncate 返回 ENOSPC）。

 **不在自动化测试中覆盖**：注入 OS 级错误需要 `LD_PRELOAD` mock 或 `tmpfs` 配额控制，
 成本过高。建议通过以下方式覆盖：
 - **手动测试**：`mount -o size=1M -t tmpfs none /dev/shm` 后运行扩容用例
 - **代码审查**：确认 `ftruncate` / `mmap` 的返回值检查路径无误
  - **未来**：若引入 fault injection 框架，优先补充此路径

 **部分失败恢复场景**（设计 §3.2 明确列出，同样因需故障注入而未覆盖）：
 - "Header 写入后、配置文件写入前失败"：shm 已更新但 config 未回填
 - "ftruncate 后 header.max_points 更新前崩溃"：文件尺寸与 header 不一致
 - "mmap 后文件尺寸与 header.max_points 不一致"：设计 §1.3 crash recovery 中有检测和修正逻辑

 这些场景需要 mid-operation crash 或文件系统注入，当前无法自动化。建议：
 - **代码审查**：验证 write-to-temp + fsync + rename 原子写入模式
 - **代码审查**：验证 crash recovery 中 `file_size > (header.max_points + 1) × BLOCK_SIZE` 修正逻辑

---

## 6. 测试覆盖矩阵

| 维度 | 用例 |
|------|------|
| 不扩容-正常 | TC1, TC2, TC3 |
| 扩容-正常 | TC4, TC5, TC6 |
| 已有点保护 | TC1, TC4, TC5（验证已有 shm_id 不变） |
| 配置解析错误 | TC7, TC8, TC9, TC10, TC11, TC12, TC22 |
| 基础设施错误 | TC13, TC14 |
| 配置回填 | TC15, TC16, TC17 |
| 状态一致性 | TC18, TC19, TC20, TC21 |
| 链式扩容 | TC23 |
| 默认 shm 过渡 | TC24 |
| 幂等性 | TC2 |
| 边界值 | TC3 (==), TC6 (大幅增长) |
| remap_version | TC20a, TC20b |
| 文件大小 | TC1（验证 7）, TC2（验证 4）, TC3（验证 4）, TC4（验证 5）, TC6（验证 3）, TC23, TC24 |
| Block 完整性 | TC1（验证 6）, TC6（验证 4）, TC21 |
