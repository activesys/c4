# C4_FUN_00055 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00055
> **对应需求**：C4_RS_00096
> **设计参考**：`docs/design/c4_shm_manager.md` §1.2 块回收, §3.2 步骤 4

C4_FUN_00055 验证 `adjust_shm` 的块回收功能——当 Writer 停止或采集点被删除，
配置文件中的点数少于共享内存中已激活的点数时，自动回收孤儿 block（state→0），
并保证仍在使用的 block 不被破坏。回收后的空闲 block 可供同一调用中的新点复用。

---

## 1. 被测功能概述

### 1.1 回收触发条件

```
required_points < header.point_count → 触发回收
```

其中 `required_points` 是配置文件中所有 Writer point 的总数，
`header.point_count` 是共享内存中 state=1 的 block 总数。

### 1.2 回收算法

```
1. 从配置文件构建 key（{service_id}.{point_id}）→ shm_id 映射
2. 扫描 block[1..max_points]，找出 state=1 的 block
3. 若 block 的 shm_id 在 key→shm_id 映射中不存在 → 孤儿块
   → state 置 0（回收），point_count 递减
4. 仍存在的 point 对应的 block 保持 state=1，不受影响
5. 回收后的空闲 block 可在同一 adjust_shm 调用中供新 point 分配
```

### 1.3 测试关键：模拟 Writer 激活

测试环境中没有真实的 Writer 进程运行，需要**直接写入共享内存**来模拟
Writer 首次写入时的 `state=1` 设置。通过 Python `mmap` + `PROT_WRITE`
写入 block 的 state 字节实现。

```python
def set_block_state(shm_path, shm_id, state):
    """设置 block[shm_id] 的 state 字段（偏移 = shm_id × 32 + 4）。"""
    offset = shm_id * 32 + 4
    fd = None
    shm = None
    try:
        fd = os.open(shm_path, os.O_RDWR)
        shm = mmap.mmap(fd, offset + 1, mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        shm.seek(offset)
        shm.write(bytes([state]))
    finally:
        if shm is not None:
            shm.close()
        if fd is not None:
            os.close(fd)
```

同时需要更新 Header 的 `point_count` 以匹配模拟的 state=1 块数：

```python
def set_header_point_count(shm_path, count):
    """设置 Header 的 point_count 字段（偏移 = 8，大端 uint32）。"""
    fd = None
    shm = None
    try:
        fd = os.open(shm_path, os.O_RDWR)
        shm = mmap.mmap(fd, 16, mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE)
        shm[8:12] = struct.pack(">I", count)
    finally:
        if shm is not None:
            shm.close()
        if fd is not None:
            os.close(fd)
```

---

## 2. 测试架构

```
c4/test/c4_fun_00055/
├── README.md                  # 本文件
├── conftest.py                # 复用 ../c4_fun_00053/conftest.py（或软链）
├── shm_helpers.py             # 复用 ../c4_fun_00053/shm_helpers.py（或软链）
└── test_reclaim.py            # 全部测试用例
```

### 2.1 被测对象 (SUT)

与 C4_FUN_00053/00054 相同：Go 编译的 `c4_shm_manager` 二进制，通过 MCP stdio JSON-RPC 通信。

### 2.2 MCP 交互序列

```
1. create_shm({instance_id}) → 初始 shm + 配置回填
2. 直接写入 shm → 模拟 Writer 激活，state=1
3. 修改配置文件 → 删除部分采集点
4. adjust_shm() → 回收孤儿块 → 返回结果
5. 直接读取 shm → 验证 block state 变化 + Header 状态 + 配置回填
```

---

## 3. 配置辅助函数

### 3.1 初始配置工厂

```python
def _make_initial_config(writer_points):
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
            "points": [
                {"key": f"device1.p{i}", "addr": 100 + i, "shm_id": 0}
                for i in range(1, writer_points + 1)
            ],
        }],
    }
    return cfg
```

### 3.2 配置修改：删除 Point

```python
def remove_points_from_config(config_path, service_id, point_ids):
    """从配置中删除指定 point_id 的采集点。"""
```

---

## 4. 测试用例

### 4.1 分支 A：纯删除回收

#### TC1: 删除部分采集点

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 5 点 → max=10，point_count=5（shm_ids 1..5）。直接写 shm 设置 block[1..5] state=1 |
| **操作** | 修改 config：删除 p3、p4、p5（保留 p1、p2），total=2 → `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | `read_shm_block`: block[1] state=1, block[2] state=1（保留的点不变） |
| **验证 2** | `read_shm_block`: block[3] state=0, block[4] state=0, block[5] state=0（孤儿回收） |
| **验证 3** | Header: `point_count=2`, `max_points=10`（不变）, `remap_version=0` |
| **验证 4** | 配置文件回填：p1→1, p2→2；p3,p4,p5 已从 config 移除 |
| **清理** | `shm_unlink + 删除临时 config` |

#### TC2: 删除整个 Writer

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 4 点 + iec104 3 点 → max=14。设置所有 7 个 block state=1 |
| **操作** | 修改 config：删除 `c4_iec104_client` section + 从 writer 列表移除。total=4 → `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | modbus block[1..4] state=1（保留） |
| **验证 2** | iec104 block[5..7] state=0（回收） |
| **验证 3** | Header: `point_count=4`, `remap_version=0` |
| **验证 4** | 配置回填：modbus shm_ids 1..4 不变 |

#### TC3: 回收 + 新增点复用

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 5 点 → max=10。设 block[1..5] state=1 |
| **操作** | 修改 config：删除 p3,p4,p5（3 点，block[3..5]），同时添加 p6,p7（2 新点，shm_id=0）。total=4 → `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | block[3..5] state=0（回收的 3 个） |
| **验证 2** | 新点 p6、p7 分配到 shm_id=3,4（从回收的 block 中复用） |
| **验证 3** | block[1] state=1, block[2] state=1（保留） |
| **验证 4** | Header: `point_count=4`, `max_points=10`, `remap_version=0` |
| **验证 5** | 配置回填：p6→3, p7→4 |

---

### 4.2 分支 B：无删除（幂等性）

#### TC4: 无删除 → 无回收

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 3 点 → max=6。设 block[1..3] state=1 |
| **操作** | 不修改 config，直接调用 `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | block[1..3] state=1（全部保持） |
| **验证 2** | Header: `point_count=3`（不变），`remap_version=0` |

#### TC5: 删除到零 → 全部回收

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 3 点 → max=6。设 block[1..3] state=1 |
| **操作** | 修改 config：清空 modbus points 数组（total=0）→ `adjust_shm()` |
| **预期** | `CONFIG_MISSING_SECTION` 错误（writer 为空属于配置错误，不是回收场景） |
| **说明** | 当前设计不允许 writer 配置为空，边界条件由配置解析层处理 |

---

### 4.3 分支 C：状态一致性

#### TC6: 回收后 Header 状态一致性

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 6 点 → max=12。设 block[1..6] state=1 |
| **操作** | 删除 p4,p5,p6（3 点）。total=3 → `adjust_shm()` |
| **验证** | `read_shm_header` 返回：`magic=0xC4DA7A00`, `version=1`, `point_count=3`, `max_points=12`, `remap_version=0` |
| **说明** | `read_shm_header` 是 `shm_helpers.py` 提供的**直接读取共享内存**函数，返回各字段的原始二进制值（magic 为 uint32 整数 `0xC4DA7A00`），不使用 `query_status` MCP 工具 |

#### TC7: 回收后配置回填 — 保留点的 shm_id 不变

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 5 点 → max=10。设 block[1..5] state=1 |
| **操作** | 删除 p3,p4（2 点）。total=3 → `adjust_shm()` |
| **验证 1** | 重新读 config：p1→1, p2→2, p5→5（p5 的 shm_id 不变，不是重编号为 3） |
| **验证 2** | Reader shm_ids 匹配保留点的 Writer shm_ids |
| **验证 3** | 非 shm_id 字段（uid, addr, key 等）未被修改 |

#### TC8: 回收后 block 完整性

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 5 点 → max=10。设 block[1..5] state=1 |
| **操作** | 删除 p4,p5（2 点）。total=3 → `adjust_shm()` |
| **验证 1** | 回收的 block[4..5]：`state=0`，`magic=0xC4DA7A00` 不变 |
| **验证 2** | 保留的 block[1..3]：magic、state=1、reserved、type 均不变 |
| **验证 3** | 空闲 block[6..10]：magic=0xC4DA7A00，state=0（不受回收影响） |

#### TC9: 部分 Writer 激活（仅部分 block state=1）

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 5 点 → max=10。仅设置 block[1], block[3], block[5] 为 state=1（模拟 Writer 未完全激活，point_count 保持 create_shm 后的默认值 5） |
| **操作** | 删除 p1（shm_id=1）。total=4，`required_points(4) < point_count(5)` → `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | block[1] state=0（p1 被删除，孤儿回收） |
| **验证 2** | block[3] state=1, block[5] state=1（保留的活跃点不变） |
| **验证 3** | block[2], block[4] state=0（从未激活的点不受影响） |
| **验证 4** | Header: `point_count=4`, `remap_version=0` |
| **说明** | 验证回收算法**仅扫描 state=1 的 block**——从未激活的 block（state=0）不管是否为孤儿都不被回收 |

#### TC10: 单点删除边界

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 6 点 → max=12。设 block[1..6] state=1 |
| **操作** | 删除 p6（1 点）。total=5 → `adjust_shm()` |
| **预期** | 返回 `"success"` |
| **验证 1** | block[6] state=0（p6 被回收） |
| **验证 2** | block[1..5] state=1（保留） |
| **验证 3** | Header: `point_count=5`, `remap_version=0` |
| **说明** | 最小删除单位——确保单个点删除触发正确回收 |

#### TC11: 序列回收（两次 `adjust_shm`，递进删除）

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm`：modbus 5 点 → max=10。设 block[1..5] state=1 |
| **操作 1** | 修改 config：删除 p4,p5（2 点）。total=3 → `adjust_shm()` |
| **验证 1a** | block[4..5] state=0，block[1..3] state=1，`point_count=3` |
| **操作 2** | 修改 config：再删除 p2,p3（2 点）。total=1 → `adjust_shm()` |
| **验证 2a** | 返回 `"success"` |
| **验证 2b** | block[2..3] state=0（第二次回收），block[1] state=1（保留），block[4..5] state=0（第一次回收结果不变） |
| **验证 2c** | Header: `point_count=1`, `remap_version=0` |
| **说明** | 验证第二次回收在已有回收结果的 shm 上正确识别新增孤儿——已回收的 block 不会因 state=0 被误操作 |

---

### 4.4 分支 D：错误路径

#### TC12: SHM 未创建 → `SHM_NOT_CREATED`

| 项目 | 内容 |
|------|------|
| **前置** | 无——不调用 `create_shm` |
| **操作** | 直接调用 `adjust_shm()`（config 文件存在） |
| **预期** | `isError: true`，错误码 `SHM_NOT_CREATED` |

#### TC13: roots/list 失败 → `CONFIG_PATH_MISSING`

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm` 成功 |
| **操作** | 调用 `adjust_shm()`，Python 对 roots/list 返回 MCP 错误 |
| **预期** | `isError: true`，错误码 `CONFIG_PATH_MISSING` |

#### TC14: 删除后 writer 为空 → `CONFIG_MISSING_SECTION`

| 项目 | 内容 |
|------|------|
| **前置** | `create_shm` 成功 |
| **操作** | 修改 config：`c4_shm_manager.writer = []`<br/>调用 `adjust_shm()` |
| **预期** | `CONFIG_MISSING_SECTION` |

---

## 5. 实现注意

### 5.1 代码复用

`conftest.py` 和 `shm_helpers.py` 软链到 `../c4_fun_00053/` 对应文件。
测试专用的 `set_block_state` 和 `set_header_point_count` 在 `test_reclaim.py` 中本地定义。

### 5.2 模拟 Writer 激活的关键约束

- block[0] 是 Header，不能设置 state。shm_id 从 1 开始
- state 字段在 block 内偏移 4 字节（block 总长 32 字节）
- 设置 state 后必须同步更新 Header 的 `point_count`，否则 `required_points < point_count` 条件不成立
- mmap 写入后立即 `msync` 不需要——MAP_SHARED 自动同步到文件

### 5.3 配置修改时序

1. `create_shm` 回填配置后，读取配置文件获取 shm_id 分配结果
2. 用 `remove_points_from_config` 删除指定 point
3. `adjust_shm` 的 roots/list 返回同一配置路径

### 5.4 回收与 shm_id 的不变性

回收仅修改 `state` 字段为 0，不改变 block 的 `magic`、`shm_id` 或其他字段。
被回收的 block 的 `shm_id` 不与原 point 解绑——该 shm_id 在原 writer 恢复时可重新激活。
测试中通过 `read_shm_block` 直接验证 state 变化，通过配置文件读取验证 shm_id 保持不变。

---

## 6. 测试覆盖矩阵

| 维度 | 用例 |
|------|------|
| 部分删除回收 | TC1, TC3, TC10 |
| 整 Writer 删除回收 | TC2 |
| 回收 + 新点复用 | TC3 |
| 无删除（幂等） | TC4 |
| 部分 Writer 激活 | TC9 |
| 序列回收 | TC11 |
| 状态一致性 | TC6, TC7, TC8 |
| 基础设施错误 | TC12, TC13 |
| 配置错误 | TC14 |
| 配置回填 | TC7 |
