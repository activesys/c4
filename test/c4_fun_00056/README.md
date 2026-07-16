# C4_FUN_00056 测试方案

> **对应功能**：`docs/specification/c4_function.md` C4_FUN_00056
> **对应需求**：C4_RS_00096
> **设计参考**：`docs/design/c4_shm_manager.md` §1.2 同时回收与再分配, §3.2 步骤 4, §4.5 场景五

C4_FUN_00056 验证 `adjust_shm` 同时进行块回收和点分配的能力——当配置文件
**既删除旧点又新增点**时，`adjust_shm` 在单次调用中先回收孤儿 block（state→0），
再从空闲块（含回收产生的）中为新点分配 shm_id，保证已有点地址不变。

---

## 1. 被测功能概述

### 1.1 统一流程

C4_FUN_00056 是 C4_FUN_00054（纯新增）和 C4_FUN_00055（纯回收）的通用情况。
`adjust_shm` 内部始终执行以下三段式流程，任一阶段在无对应操作时自然成为 no-op：

```
1. 回收阶段（始终执行）：
   - 构建配置中 Writer key → shm_id 映射
   - 扫描 shm 中所有 state=1 block
   - 孤儿 block（shm_id 不在映射中）→ state 置 0，point_count 递减
   - 仍存在的 block 保持 state=1
   （若配置中无删除点 → 孤儿集为空 → no-op）
2. 容量判断：
   - required_points ≤ max_points → 跳过扩容
   - required_points > max_points → 扩容至 required_points × 2
3. 分配阶段（始终执行）：
   - 扫描 state=0 的空闲 block（含回收产生的）
   - 为配置中 shm_id=0 的新点分配空闲 shm_id
   - 已有点保持原 shm_id
   （若配置中无新点 → shm_id=0 集合为空 → no-op）
```

### 1.2 关键约束：先回收再分配

回收必须在分配之前执行。若违规，则新点可能分配到尾部空闲块而非刚回收的低地址空间，
更严重的是，若实现错误地复用被删点的 shm_id 而不先回收，后续回收阶段会误判孤儿导致数据丢失。

### 1.3 全量回收

当配置中 `c4_shm_manager.writer` 为空（所有 Writer 已删除）时，所有 state=1 block
均为孤儿，全部回收为 state=0，point_count 归零。共享内存保留，不 shm_unlink，
供后续 adjust_shm 重新分配。此场景 `adjust_shm` 不返回 CONFIG_MISSING_SECTION。

### 1.4 测试关键：模拟 Writer 激活

测试环境中没有真实的 Writer 进程运行，通过 Python `mmap` + `PROT_WRITE`
直接写入共享内存来模拟 Writer 首次写入时的 `state=1` 设置：

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
c4/test/c4_fun_00056/
├── README.md                  # 本文件
├── conftest.py                # 软链 → ../c4_fun_00053/conftest.py
├── shm_helpers.py             # 软链 → ../c4_fun_00053/shm_helpers.py
└── test_mixed.py              # 全部测试用例
```

### 2.1 被测对象 (SUT)

Go 编译的 `c4_shm_manager` 二进制，通过 MCP stdio JSON-RPC 通信。

### 2.2 MCP 交互序列

```
1. create_shm({instance_id}) → 初始 shm + 配置回填
2. 直接写入 shm → 模拟 Writer 激活，state=1
3. 修改配置文件 → 删除旧点 + 添加新点
4. adjust_shm() → 回收孤儿块 → 分配新点 → 返回结果
5. 直接读取 shm → 验证 block state + Header + 配置回填
```

### 2.3 验证方式

全部通过 `read_shm_header` / `read_shm_block` 直接读共享内存验证，不依赖 `query_status`。

---

## 3. 测试用例

### 3.1 测试起点

每个测试起点为 `create_shm` 后的初始状态：

```
writer1(modbus#1):   [1..2]   (2点)
writer2(modbus#2):   [3..4]   (2点)
writer3(iec104#1):   [5..7]   (3点)
writer4(iec104#2):   [8..10]  (3点)
─────────────────────────────────
point_count = 10
max_points  = 20
空闲: [11..20] (10个, state=0)
```

**通用步骤**：

```
1. 调用 create_shm → "success"
2. 直接写入 shm 设置所有 block state=1，更新 header.point_count=10
3. 修改配置文件（删除旧点 + 添加新点）
4. 调用 adjust_shm → 验证结果
5. 直接读取 shm 验证 block state、header、配置回填
```

---

### TC1 · 等量置换（删除 n 点 + 新增 n 点，无扩容）

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer3（3 点），新增 writer5（3 点）→ required_points=10 |
| **预期** | `adjust_shm` 返回 `"success"` |
| **回收** | writer3 的 block[5..7] state→0，point_count: 10→7 |
| **分配** | writer5 的 3 个新点从 [5..7] 分配（复用回收空间），point_count: 7→10 |
| **验证** | ① Header: point_count=10, max_points=20（不变）<br/>② block[1..2]: state=1, shm_id=1,2（writer1 不变）<br/>③ block[3..4]: state=1, shm_id=3,4（writer2 不变）<br/>④ block[5..7]: state=1, shm_id=5,6,7（writer5 新点，复用原 writer3 位置）<br/>⑤ block[8..10]: state=1, shm_id=8,9,10（writer4 不变）<br/>⑥ block[11..20]: state=0（空闲）<br/>⑦ 配置中 writer5 的 shm_id 被回填为 5,6,7 |

---

### TC2 · 净增（删除 n 点 + 新增 m 点，n<m，无扩容）

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer3（3 点），新增 writer5（5 点）→ required_points=12 |
| **预期** | `adjust_shm` 返回 `"success"` |
| **回收** | writer3 的 block[5..7] state→0，point_count: 10→7 |
| **分配** | required_points(12)≤max_points(20) 不扩容<br/>空闲: [5..7]+[11..20]=13 个<br/>顺序扫描，writer5 的 5 点分配 → [5..7]+[11..12]，point_count: 7→12 |
| **验证** | ① Header: point_count=12, max_points=20<br/>② block[5..7]: state=1, shm_id=5,6,7（复用回收空间）<br/>③ block[11..12]: state=1, shm_id=11,12（新分配）<br/>④ block[8..10]: state=1（writer4 不变）<br/>⑤ block[13..20]: state=0（空闲）<br/>⑥ 配置中 writers 的 shm_id 回填正确 |

---

### TC3 · 净减（删除 n 点 + 新增 m 点，n>m，无扩容）

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer3+writer4（3+3=6 点），新增 writer5（4 点）→ required_points=8 |
| **预期** | `adjust_shm` 返回 `"success"` |
| **回收** | block[5..10] state→0，point_count: 10→4 |
| **分配** | required_points(8)≤max_points(20) 不扩容<br/>空闲: [5..10]+[11..20]=16 个<br/>writer5 的 4 点分配 → [5..8]，point_count: 4→8 |
| **验证** | ① Header: point_count=8, max_points=20<br/>② block[5..8]: state=1（writer5 复用回收空间）<br/>③ block[9..10]: state=0（多余回收，保持空闲）<br/>④ block[1..4]: state=1（writer1,2 不变）<br/>⑤ 配置中 writer5 的 shm_id 回填正确 |

---

### TC4 · 混合扩容（删除 n 点 + 新增 m 点，触发扩容）

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer3（3 点），新增 writer5（15 点）→ required_points=22 |
| **预期** | `adjust_shm` 返回 `"success"` |
| **回收** | block[5..7] state→0，point_count: 10→7 |
| **扩容** | required_points(22)>max_points(20)<br/>new_max=22×2=44<br/>ftruncate, remap_version++, munmap+mmap |
| **分配** | 空闲: [5..7]+[11..44]=37 个（扩容空间：block[21..44] state=0）<br/>writer5 的 15 点 → [5..7]+[11..22]，point_count: 7→22 |
| **验证** | ① Header: point_count=22, max_points=44, remap_version++<br/>② block[21..44]: magic 全部为 0xC4DA7A00<br/>③ block[5..7]: state=1（复用回收）<br/>④ block[11..22]: state=1（部分从旧空闲 + 扩容空间）<br/>⑤ block[1..4][8..10]: state=1（writer1,2,4 不变）<br/>⑥ 已有点 block 在扩容前后物理地址不变 |

---

### TC5 · 单点置换（删除 1 点 + 新增 1 点）

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer1 的第 2 个点（block[2]），新增 writer5（1 点）→ required_points=10 |
| **预期** | `adjust_shm` 返回 `"success"` |
| **回收** | block[2] state→0，point_count: 10→9 |
| **分配** | writer5 的 1 点从 block[2] 分配（block[1] 为 writer1 占用，扫描跳过），point_count: 9→10 |
| **验证** | ① point_count=10（不变），max_points=20<br/>② block[1]: state=1（writer1 保留的点不变）<br/>③ block[2]: state=1, shm_id=2（writer5 新点复用）<br/>④ block[3..10]: state=1（writer2,3,4 完全不变）<br/>⑤ block[11..20]: state=0（空闲）<br/>⑥ 配置回填正确

---

### TC6 · 全量回收（writer 为空，所有 block 回收）

| 项目 | 内容 |
|------|------|
| **操作** | 修改 config：`c4_shm_manager.writer=[]`, `c4_shm_manager.reader=[]` → adjust_shm() |
| **预期** | `adjust_shm` 返回 `"success"`（**不返回** CONFIG_MISSING_SECTION） |
| **回收** | 所有 state=1 block[1..10] → state=0，point_count: 10→0 |
| **分配** | 无新点，no-op |
| **验证** | ① Header: point_count=0, max_points=20（不变）<br/>② block[1..10]: state=0, magic=0xC4DA7A00（回收后保持）<br/>③ block[11..20]: state=0（原空闲不变）<br/>④ shm 文件仍存在（未 unlink）<br/>⑤ 共享内存可被后续 adjust_shm 重新分配 |

---

### TC7 · 全量回收后重新分配

| 项目 | 内容 |
|------|------|
| **前置** | TC6 执行完毕，shm 处于 point_count=0 状态 |
| **操作** | 修改 config：添加 writer5（5 点）→ adjust_shm() |
| **预期** | `adjust_shm` 返回 `"success"` |
| **分配** | required_points(5)≤max_points(20)，不扩容<br/>writer5 的 5 点从 [1..5] 分配（从 shm_id=1 开始顺序扫描 state=0） |
| **验证** | ① Header: point_count=5, max_points=20<br/>② block[1..5]: state=1, shm_id=1..5（全新分配）<br/>③ block[6..20]: state=0<br/>④ 配置回填正确 |

---

### TC8 · 扩容后混合（先纯新增扩容，再混合操作）

| 项目 | 内容 |
|------|------|
| **前置** | 首次 adjust_shm：新增 writer5（15 点）→ 扩容至 max_points=50，point_count=25 |
| **操作** | 第二次 adjust_shm：删除 writer3（3 点）+ 新增 writer6（8 点）→ required_points=30 |
| **预期** | `adjust_shm` 返回 `"success"` |
| **回收** | block[5..7] state→0，point_count: 25→22 |
| **分配** | required_points(30)≤max_points(50) 不扩容<br/>writer6 的 8 点从 state=0 分配，point_count: 22→30 |
| **验证** | ① Header: point_count=30, max_points=50（不二次扩容）<br/>② block[5..7]: state=1（writer6 复用原 writer3 位置）<br/>③ writer1,2,4,5 的 block 完全不受影响<br/>④ 扩容后的大 shm 生命周期内混合操作正常 |

---

### TC9 · 已有点完全不受影响（交叉验证）

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer2（2 点）+ 新增 writer5（2 点）→ adjust_shm() |
| **验证** | 对每个被保留的 writer 逐点验证：<br/>① writer1: block[1..2] state=1, shm_id=1,2 不变<br/>② writer3: block[5..7] state=1, shm_id=5,6,7 不变<br/>③ writer4: block[8..10] state=1, shm_id=8,9,10 不变<br/>④ writer2 的 block[3..4] state=0（已回收）<br/>⑤ writer5 的新点从 [3..4]（刚回收）分配 |

---

### TC10 · Block 完整性验证

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer3（3 点）+ 新增 writer5（3 点）→ adjust_shm() |
| **验证** | 对所有 block[1..20] 逐块验证：<br/>① block[1..4]: magic=0xC4DA7A00, state=1, shm_id 与 create_shm 初始值一致<br/>② block[5..7]: magic=0xC4DA7A00, state=1, shm_id 回填与配置一致<br/>③ block[8..10]: magic=0xC4DA7A00, state=1, shm_id 与初始一致<br/>④ block[11..20]: magic=0xC4DA7A00, state=0<br/>⑤ 无块出现 magic 损坏或 state 不一致 |

---

### TC11 · 配置回填验证

| 项目 | 内容 |
|------|------|
| **操作** | 删除 writer3（3 点）+ 新增 writer5（3 点）→ adjust_shm() |
| **验证** | 读回配置文件，检查：<br/>① writer5 所有 point 的 shm_id 已从 0 回填为 ≥1 的实际值<br/>② writer1,2,4 的 point 的 shm_id 保持原值不变<br/>③ writer3 的 point 已从配置中移除<br/>④ 无 shm_id=0 残留<br/>⑤ Reader 的 shm_id 引用被正确更新 |

---

### TC12 · SHM_NOT_CREATED

| 项目 | 内容 |
|------|------|
| **操作** | 不调用 `create_shm`，直接调用 `adjust_shm()` |
| **预期** | `SHM_NOT_CREATED` 错误 |

---

### TC13 · CONFIG_PATH_MISSING

| 项目 | 内容 |
|------|------|
| **前置** | create_shm 后，Agent 不响应 roots/list |
| **操作** | `adjust_shm()` |
| **预期** | `CONFIG_PATH_MISSING` 错误 |

---

### TC14 · CONFIG_MISSING_SECTION

| 项目 | 内容 |
|------|------|
| **操作** | 修改 config：设置 `c4_shm_manager.writer=[]` 但 `c4_shm_manager.reader` 非空 → adjust_shm() |
| **预期** | `CONFIG_MISSING_SECTION` 错误（仅一方为空，不符合双方均为空或双方均非空的约束） |

---

### TC15 · DUPLICATE_KEY

| 项目 | 内容 |
|------|------|
| **操作** | 新增 writer5 时，其 point_id 与 writer1 的某个 point_id 相同（同一 service_id 下）→ adjust_shm() |
| **预期** | `DUPLICATE_KEY` 错误 |

---

### TC16 · UNKNOWN_READER_KEY

| 项目 | 内容 |
|------|------|
| **操作** | 修改 Reader 的 key 引用一个已被删除的 Writer point → adjust_shm() |
| **预期** | `UNKNOWN_READER_KEY` 错误 |

---

## 4. 用例与场景映射

| 场景分类 | 用例 |
|----------|------|
| **基本混合操作** | TC1（等量置换），TC2（净增），TC3（净减） |
| **扩容混合** | TC4（扩容），TC8（扩容后二次混合） |
| **边界** | TC5（单点置换），TC6（全量回收），TC7（全量回收后重新分配） |
| **状态一致性** | TC9（已有点不变），TC10（block 完整性），TC11（配置回填） |
| **错误路径** | TC12（shm 未创建），TC13（config path missing），TC14（一方为空），TC15（key 冲突），TC16（reader key 不存在） |

---

## 5. 与前序功能的关系

| 功能 | 关系 | 说明 |
|------|------|------|
| C4_FUN_00054 | 退化情况 | 纯新增 = C4_FUN_00056 回收阶段 no-op |
| C4_FUN_00055 | 退化情况 | 纯回收 = C4_FUN_00056 分配阶段 no-op |
| C4_FUN_00056 | **通用情况** | 回收 + 分配均活跃 |

三个功能共用同一个 `adjust_shm` 工具，C4_FUN_00056 验证的是两阶段均活跃的混合路径，
是 00054 和 00055 的超集。00054 和 00055 的测试保持独立以验证各自退化路径的正确性。
