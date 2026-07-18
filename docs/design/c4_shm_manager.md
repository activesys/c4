# C4 共享内存管理设计

> **版本**：v0.3.1 | **最后更新**：2026-07-16 | **父文档**：[c4_architecture.md](c4_architecture.md)

---

本文档描述 `c4_shm_manager` MCP 服务的详细设计，包括共享内存的创建、扩容、点分配、
配置文件解析、MCP 工具接口和错误码。共享内存的布局定义、并发协议和整体架构见
[c4_architecture.md](c4_architecture.md)。

---

## 1. 共享内存生命周期

共享内存由 **`c4_shm_manager`** 创建并管理。`c4_shm_manager` 是每个 C4 实例默认启动
的首个 MCP 服务，它通过 MCP 协议向 Agent 暴露共享内存管理的全部能力（创建、扩容、
块分配/回收）。Agent 不直接操作共享内存——所有 shm 操作通过
`c4_shm_manager` 提供的 MCP 工具完成。

### 1.1 创建流程

```
Agent 生成 instance_id
        │
        │ MCP 工具调用 c4_shm_manager.create_shm
        ▼
┌───────────────────────────────────────────────┐
│            c4_shm_manager（Go）                 │
│                                               │
│  shm_open("/c4_{id}", O_CREAT|O_EXCL|O_RDWR)  │
│  ftruncate(shm_size)                           │
│  mmap                                         │
│  初始化 Header（magic, version, max_points）    │
│  写入所有 Data Block 的 magic = 0xC4DA7A00      │
│  写入 Header magic = 0xC4DA7A00 （最后）          │
│  返回创建结果 → Agent                          │
└───────────────────────────────────────────────┘
        │
        │ Agent 通过 MCP 协议启动其他 MCP 服务
        ▼
┌───────────────────────────────────────────────┐
│          其他 MCP 服务启动（Go）                 │
│  shm_open("/c4_{id}", O_RDWR)   // 不传 O_CREAT│
│  mmap                                         │
│  校验 magic == 0xC4DA7A00                      │
│  开始读写共享内存                               │
└───────────────────────────────────────────────┘
```

```mermaid
flowchart TD
    A["Agent 生成 instance_id"] --> B["MCP 调用<br/>c4_shm_manager.create_shm"]
    B --> C["shm_open(/c4_{id},<br/>O_CREAT|O_EXCL|O_RDWR)<br/>ftruncate + mmap<br/>初始化 Header +<br/>Data Block Array"]
    C --> D["Agent 启动其他<br/>MCP 服务"]
    D --> E["后续 MCP 服务<br/>shm_open(O_RDWR)<br/>mmap · 校验 magic"]
    E --> F["所有 MCP 服务运行<br/>读写共享内存"]
    F --> G{"Agent 停止 MCP?"}
    G -->|"是"| H["各 MCP 服务 munmap"]
    H --> I["c4_shm_manager<br/>最后退出 → shm_unlink"]
```

| 操作 | 说明 |
|------|------|
| 创建 | Agent 通过 MCP 工具调用 `c4_shm_manager`，命名规则 `/c4_{instance_id}` |
| 附加 | 后续 MCP 服务以普通 `O_RDWR` 或 `O_RDONLY` 打开，校验 `magic` 后附加 |
| 大小 | 无配置文件时默认 100k 点（≈3 MB）；配置文件存在时按 §2.2 算法计算，Agent 可通过 MCP 工具调整 |
| 销毁 | `c4_shm_manager` 最后退出时 `shm_unlink`；进程异常退出由操作系统回收 |

### 1.2 扩容与块分配管理

#### adjust_shm 统一调整机制

当用户新增采集点或启动新 Writer 后，或者停掉 Writer / 删除采集点后，Agent 更新配置文件，然后调用
`c4_shm_manager` 的 `adjust_shm` 工具一次性完成容量判断、扩容（如需）、点分配和块回收。
所有操作在 `c4_shm_manager` 进程内原子化完成。

**Stop-Start 协议前置**：为确保 `ftruncate` / `munmap` 期间其他 MCP 进程不访问
共享内存（避免 SIGSEGV），Agent 调用 `adjust_shm` 前必须先执行 Stop-Start 协议的
Stop 阶段（stop/start 工具定义见 [c4_architecture.md §3.3.1](c4_architecture.md)）。流程如下：

```
Phase 1 - Stop：
  a. Agent 向所有 MCP 进程下发 stop 指令
  b. 每个 MCP 进程：
     - 关闭所有监听端口和活跃连接
     - 销毁实例状态
     - 向 Agent ack "success"

Phase 2 - adjust_shm：
   a. Agent 确认所有 MCP 进程已停止 → 调用 c4_shm_manager.adjust_shm()
   b. c4_shm_manager 内部：
      - 通过 roots/list 获取配置文件路径
      - 读取配置文件，按 §2.2 算法计算所需点数 (required_points)
         ┌ 回收孤儿块（扫描 state=1 block，按 key 匹配判定孤儿）：
         │   遍历配置文件中的所有 Writer point，构建 key → shm_id 映射
         │   扫描 shm 中所有 state=1 的 block
         │   若 block 的 shm_id 在 key→shm_id 映射中不存在 → 孤儿块，state 置 0
         │   若 block 的 shm_id 在映射中存在 → 保持 state=1，不受影响
         │   注意：回收条件不依赖 required_points 与 point_count 的大小关系，
         │   即使新增点数多于删除点数（required_points ≥ point_count），
         │   只要配置中删除了某些 key，对应的孤儿 block 仍会被回收
         ├ 若 required_points ≤ current_max_points（不超容量，回收后或纯新增）：
         │   在 state=0 的空闲块中（含回收产生的空闲块）为新点分配 shm_id
         │   已有点地址不变
         │   header.point_count = required_points
         └ 若 required_points > current_max_points（超容量）：
            new_max = required_points × 2
            ftruncate(shm_fd, (new_max + 1) × 32)
            header.max_points = new_max
            munmap + mmap 新大小
            新增 block 全部写入 magic = 0xC4DA7A00
            已有点保持原 shm_id，配置中 shm_id=0 的新点分配空闲 shm_id
            header.point_count = required_points
      - 回填配置文件（将分配后的 shm_id 写入各 MCP Server 的 points 数组）
      - 写回配置文件到磁盘
      - 返回结果给 Agent

Phase 3 - Start：
  a. Agent 向所有 MCP 进程下发 start 指令
  b. 每个 MCP 进程：
     - 重新 shm_open + mmap 共享内存（获取调整后的大小）
     - 重新读取配置文件
     - 启动所有数据路径实例
```

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant M1 as modbus_client
    participant M2 as asfp2_client
    participant M3 as 其他 MCP ...

    A->>M1: stop
    A->>M2: stop
    A->>M3: stop
    M1->>M1: 停止循环<br/>等待临界区退出
    M2->>M2: 停止循环<br/>等待临界区退出
    M3->>M3: 停止循环<br/>等待临界区退出
    M1-->>A: "success"
    M2-->>A: "success"
    M3-->>A: "success"

    Note over A: 所有进程已暂停<br/>shm 无访问

    A->>S: adjust_shm()
    S->>A: roots/list
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}
    S->>S: 读配置，算点数<br/>容量判断
    Note over S: 先回收孤儿块<br/>（按 key 匹配，不按计数）
    alt 不超容量
        Note over S: 空闲块（含回收的）中分配<br/>已有点不变
    else 超容量
        S->>S: ftruncate<br/>max_points=N×2<br/>munmap + mmap<br/>已有点保持原ID, 新点分配
    end
    S->>S: 回填配置 → 写回磁盘
    S-->>A: "success"

    A->>M1: start
    A->>M2: start
    A->>M3: start
    M1->>M1: shm_open + mmap<br/>重新读配置 → 启动实例
    M2->>M2: shm_open + mmap<br/>重新读配置 → 启动实例
    M3->>M3: shm_open + mmap<br/>重新读配置 → 启动实例
```

**`reserved`**：`stop` 已释放所有进程的 shm 映射，`start` 总是 fresh `shm_open` + `mmap` 自动获取最新大小。`reserved` 字段不再需要被检测或比对，始终为 0。

**扩容开销**：扩容过程中数据断流时长 ≈ stop 关闭时间（< 1ms）+ ftruncate + mmap（~100μs）+ start 重启（< 1ms），总计 < 5ms。扩容是低频事件（数天至数月一次），可接受。

**关键性质**：

- **配置文件是唯一真相源**：`adjust_shm` 以配置文件中的 points 列表为准计算需求和分类 block（已有点 vs 孤儿块 vs 空闲块），不依赖 Agent 传入点数参数
- **已有点地址不变**：`ftruncate` 只追加尾部空间，已有 block 的物理偏移不变。配置文件中已有点的 shm_id 始终保持原值
- **shm_id 一次分配，终身不变**：寻址公式绑定物理位置，重编号会导致映射关系全乱。即使 writer 停止或点被删除，其 shm_id 不会被重新分配给其他点——该 block 被回收后仅供同一点重新激活使用
- **不缩容**：已分配后不再缩减共享内存，避免截断仍在使用的块。块回收仅将 `state` 置 0，不释放文件空间。空间换简单性
- **回收通过 adjust_shm 统一处理**：Writer 停止或点表缩减时，`adjust_shm` 将不再出现在配置中的 point 对应的 block 的 `state` 置 0，`point_count` 随之递减。回收后 block 可被后续分配复用
- **全量离线可重建**：如果碎片严重，可在所有 writer 离线时 `shm_unlink` 后重新紧凑分配

#### 块回收

回收发生在 writer 停止或需要减少采集点时，**由 `adjust_shm` 统一处理**。必须确保 write 和 read
都已停止（通过 Stop-Start 协议，见 §1.2）后才修改 `state`，避免与新分配的 writer 冲突。

**回收算法**：

`adjust_shm` 读取配置文件后，对比当前 shm 中已分配的 block 与配置文件中的 Writer point 集合：

1. 遍历当前 shm 中所有 `state=1` 的 block，其 shm_id 对应配置中某个已分配的 point
2. 配置文件中的 point 有确定的 `key`（`{service_id}.{point_id}`），通过 key→shm_id 映射找到对应 block
3. 若某个 block 的 shm_id 在配置文件中**找不到对应 point**（即该 point 已被删除），则此 block 为**孤儿块**：
   - block 的 `state` 置 0（回收为空闲）
   - `point_count` 递减
4. 仍存在的 point 对应的 block 保持 `state=1`，不受影响
5. 回收后的空闲 block 可在同一 `adjust_shm` 调用中被新 point 分配（先回收再分配）

**全量回收**：当配置文件中 `c4_shm_manager.writer` 为空（所有 Writer 已删除，Reader 也已全部停止），
则所有 `state=1` 的 block 均为孤儿块，全部回收为 `state=0`，`point_count` 归零。共享内存本身保留
（不 `shm_unlink`），供后续 `adjust_shm` 重新分配。此场景 `adjust_shm` 不返回
`CONFIG_MISSING_SECTION` 错误——writer 为空在回收上下文中是合法的。

**`point_count` 的维护**：由于只有 `c4_shm_manager`（受 Agent 委托）做分配/回收操作，
`point_count` 的读写是无竞争的，无需原子操作保护。

#### Writer 替换示例

```
初始状态（create_shm 后，max_points=20）：
  writer1: [ 1..2]   (2个, state=1)
  writer2: [ 3..4]   (2个, state=1)
  writer3: [ 5..7]   (3个, state=1)
  writer4: [ 8..10]  (3个, state=1)
  空闲:    [11..20]  (10个, state=0)
  point_count=10

Agent 新增 writer5(15个)，生成新配置文件：

1. Agent → 所有 MCP: stop

2. Agent → c4_shm_manager: adjust_shm()
   required_points = 10 + 15 = 25
   current_max_points = 20
   required_points(25) > current_max_points(20) → 扩容
   new_max = 25 × 2 = 50
    ftruncate → max_points=50
    分配：已有 [1..10] 不变，新增 [11..25]
   空闲：[26..50] (25个, state=0)
   point_count=25
   回填配置，返回 "success"

3. Agent → 所有 MCP: start

最终：
  writer1:  [ 1..2]   (2个, 不变)
  writer2:  [ 3..4]   (2个, 不变)
  writer3:  [ 5..7]   (3个, 不变)
  writer4:  [ 8..10]  (3个, 不变)
  writer5:  [11..25]  (15个, 新增)
  空闲:    [26..50]  (25个)
  point_count=25
```

关键性质：
- **已有点全部保持原 shm_id**：writer1~4 的 shm_id 不变，数据管道无需重配置
- **碎片可接受**：`shm_id * 32` 仍是 O(1) 直接寻址

#### Writer 删除回收示例

```
初始状态（write1~4 运行中，max_points=20）：
  writer1:  [ 1..2]   (2个, state=1)
  writer2:  [ 3..4]   (2个, state=1)
  writer3:  [ 5..7]   (3个, state=1)
  writer4:  [ 8..10]  (3个, state=1)
  空闲:    [11..20]  (10个, state=0)
  point_count=10

Agent 停止 writer3(3个点)，从配置文件删除 writer3 的 points：

1. Agent → 所有 MCP: stop

2. Agent → c4_shm_manager: adjust_shm()
   required_points = 2 + 2 + 3 = 7  (writer1+2+4)
   current_max_points = 20
   required_points(7) < point_count(10) → 触发回收

   回收阶段：
     配置文件 key 集合 = {writer1.p1, writer1.p2, writer2.p1, writer2.p2,
                         writer4.p1, writer4.p2, writer4.p3}
     writer3 的 3 个 key 不在配置中 → 对应 block[5..7] state → 0
     point_count: 10 → 7

   分配阶段：无新 point（全部已有 shm_id），跳过

   最终：
     writer1:  [1..2]   (state=1)
     writer2:  [3..4]   (state=1)
     已回收:  [5..7]   (state=0，原 writer3)
     writer4:  [8..10]  (state=1)
     空闲:    [5..7][11..20]  (13个, state=0)
     point_count=7
   回填配置，返回 "success"

3. Agent → 所有 MCP: start
```

关键性质：
- **回收不破坏仍在用的 block**：writer1、2、4 的 block 保持 state=1，数据管道不受影响
- **回收后可立即复用**：回收的 block[5..7] 在同一 `adjust_shm` 调用中可供新 point 分配
- **shm_id 不重分配**：block[5] 的 shm_id 始终是 5，即使被回收也不会分配给其他 key——仅在原 writer3 恢复时重新激活

#### 同时回收与再分配

当 Agent 在一次配置变更中**既删除旧点又新增点**时，`adjust_shm` 在一次调用中顺序执行
回收 → 分配，保证操作的完整性和已有点的地址安全。此场景的关键约束是**先回收再分配**，
回收产生的空闲 block 可立即复用于新增点。

**算法**：

```
1. 回收阶段（与 §1.2 块回收算法相同）：
   - 构建配置中 Writer key → shm_id 映射
   - 扫描 shm 中所有 state=1 block
   - 孤儿 block（shm_id 不在映射中）→ state 置 0，point_count 递减
   - 仍存在的 block 保持 state=1

2. 分配阶段：
   - 扫描 state=0 的空闲 block（含回收产生的）
   - 为配置中 shm_id=0 的新点分配空闲 shm_id
   - 已有点保持原 shm_id，不受影响
   - header.point_count = required_points
```

**示例一：删除 writer3(3点) + 新增 writer5(3点)**

```
初始状态（max_points=20）：
  writer1:  [ 1..2]   (2个, state=1)
  writer2:  [ 3..4]   (2个, state=1)
  writer3:  [ 5..7]   (3个, state=1)   ← 将被删除
  writer4:  [ 8..10]  (3个, state=1)
  空闲:    [11..20]  (10个, state=0)
  point_count=10

Agent 删除 writer3 并新增 writer5(3点)，生成新配置文件，调用 adjust_shm()：

  required_points = 2 + 2 + 3 + 3 = 10  (writer1+2+4+5)
  current_max_points = 20

  回收阶段（先执行）：
    配置 key 集合包含 writer5 的 3 个新 key，不含 writer3 的 3 个旧 key
    → writer3 的 block[5..7] 为孤儿 → state 置 0
    point_count: 10 → 7

  分配阶段（后执行）：
    扫描 state=0 block: [5..7]（刚回收）+ [11..20]（原有空闲）
    → writer5 的 3 个新点从 [5..7] 分配（复用回收空间）
    point_count: 7 → 10

  最终：
    writer1:  [1..2]   (state=1, 不变)
    writer2:  [3..4]   (state=1, 不变)
    writer5:  [5..7]   (state=1, 新增，复用原 writer3 位置)
    writer4:  [8..10]  (state=1, 不变)
    空闲:    [11..20]  (10个, state=0)
    point_count=10  （与初始相同）

   关键性质：
   - 回收→分配的自然结果：point_count 先降后升（10→7→10），最终不变
   - 已有点（writer1,2,4）的 shm_id 和地址完全不受影响
```

**示例二：删除 writer3(3点) + 新增 writer5(5点)，净增 2 点**

```
初始状态（max_points=20）：
  writer1:  [ 1..2]   (2个, state=1)
  writer2:  [ 3..4]   (2个, state=1)
  writer3:  [ 5..7]   (3个, state=1)   ← 将被删除
  writer4:  [ 8..10]  (3个, state=1)
  空闲:    [11..20]  (10个, state=0)
  point_count=10

Agent 删除 writer3 并新增 writer5(5点)，调用 adjust_shm()：

  required_points = 2 + 2 + 3 + 5 = 12

  回收阶段：
    writer3 的 block[5..7] → state=0
    point_count: 10 → 7

  分配阶段：
    required_points(12) ≤ max_points(20)，不扩容
    空闲 block: [5..7] + [11..20] = 13 个（[8..10] 被 writer4 占用，不参与）
    顺序扫描，writer5 的 5 个新点分配 → [5..7] + [11..12]
    point_count: 7 → 12

  最终：
    writer1:  [ 1..2]   (state=1)
    writer2:  [ 3..4]   (state=1)
    writer5:  [ 5..7]   (state=1, 复用回收空间)
              [11..12]  (state=1, 新分配)
    writer4:  [ 8..10]  (state=1, 不变)
    空闲:    [13..20]  (8个, state=0)
    point_count=12
```

**错误场景**：如果违规**先分配再回收**，则新点会从空闲块尾部（而非刚回收的位置）分配，
回收产生的空闲块被浪费在低地址区。更严重的，若实现以"净点数不变"为由直接
复用被删除点的 shm_id 而不执行回收，后续 key 匹配的回收阶段会将复用后的 block 误判为
孤儿并置 state=0，导致新分配的点丢失。因此 `adjust_shm` 必须严格遵守
**先回收、再分配**的顺序。

### 1.3 `c4_shm_manager` 崩溃恢复

`c4_shm_manager` 崩溃后，POSIX 共享内存对象 `/c4_{id}` 仍然存在于 `/dev/shm`
（崩溃进程未调用 `shm_unlink`），其他 MCP 进程的 mmap 映射不受影响。

重启时不能走 `O_CREAT|O_EXCL` 路径——名字已被占用。流程如下：

```
c4_shm_manager 重启
        │
        │ shm_open("/c4_{id}", O_RDWR)  // 不传 O_CREAT
        │ 若失败（文件不存在或权限错误）→ shm_unlink + O_CREAT|O_EXCL 新建
        ▼
┌───────────────────────────────────────────────┐
│              附加已有共享内存                   │
│                                               │
│  mmap                                         │
│  校验 Header magic == 0xC4DA7A00              │
│  ┌─ 若 magic 无效：shm_unlink + 重建新 shm     │
│  └─ 若 magic 有效：                           │
│       扫描 block[1..max_points]               │
│         count = 0                             │
│         for i = 1..max_points:                │
│           if block[i].state == 1: count++     │
│         header.point_count = count            │
│        → 以 state=1 为唯一权威源重建 point_count│
│                                               │
│  恢复正常服务（可接受 alloc/free 请求）          │
└───────────────────────────────────────────────┘
```

关键保证：

- **其他 MCP 进程不受影响**：它们的 mmap 在 crash 期间始终有效，不会 SIGSEGV
- **`point_count` 自动修复**：扫描重建，不依赖崩溃前的内存值
- **`state=1` 是权威源**：即使崩溃发生在 alloc 后、writer 首次写入前（block 已分配但 state 仍为 0），重建后这些 block 自然为"空闲"，下次分配时回收——不浪费
- **始终复用而非重建**：重建意味着 `shm_unlink`，会导致其他进程的 mmap 失效后 SIGSEGV。只有 magic 校验失败（shm 真的损坏了）才走重建路径

---

## 2. 配置文件解析与 shm_id 分配算法

### 2.1 流程概述

Agent 与 `c4_shm_manager` 的交互遵循 MCP 标准协议。启动 `c4_shm_manager` 并创建共享内存的
完整流程如下：

```
1. Agent 启动 c4_shm_manager 进程
2. Agent 使用 MCP 协议初始化 c4_shm_manager，获得工具列表
   （Agent 须在 initialize 中声明 roots 能力，否则 c4_shm_manager 无法发送 roots/list 请求）
3. Agent 调用 c4_shm_manager 的 create_shm 工具（传入 instance_id）
4. c4_shm_manager 向 Agent 发送 roots/list 请求（MCP 协议中 server→client 的根目录查询）
   → Agent 返回配置文件的绝对路径 URI（含文件名）。文件名和路径可配置，例如
   `/etc/c4/shm.json` 或 `~/.local/c4/shared_memory.json`
5. c4_shm_manager 根据配置文件是否存在决定共享内存大小：
   - **配置文件不存在或为 null**（roots/list 返回空、文件路径不存在、或文件内容为空 JSON）：
     创建默认 10 万点共享内存空间（max_points = 100000，point_count = 0），
     所有 Data Block 初始化为 magic=0xC4DA7A00、state=0，
     此时无 Writer/Reader 配置，不涉及 shm_id 分配和配置回填
    - **配置文件存在**：
      读取并解析配置文件：
        ┌ 若 `c4_shm_manager.writer` 和 `c4_shm_manager.reader` 均为空 → 视为无配置，
        │   创建默认 10 万点共享内存（max_points = 100000, point_count = 0），
        │   不涉及 shm_id 分配和配置回填
        ├ 若仅一方为空（writer 空但 reader 非空，或反之）→ 返回 `CONFIG_MISSING_SECTION` 错误
        └ 若双方均非空 → 按 §2.2 算法计算所需大小，创建并初始化共享内存空间，
            分配 shm_id 并回填配置文件
6. c4_shm_manager 向 Agent 返回 create_shm 的调用结果
```

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant FS as config.json

    A->>S: 启动进程
    A->>S: initialize (MCP 握手, 声明 roots 能力)
    S-->>A: 工具列表 (create_shm, adjust_shm, ...)

    A->>S: tools/call (create_shm)<br/>params: {instance_id}
    S->>A: roots/list (请求根目录)
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}

    alt 配置文件存在
        S->>FS: 读取配置文件, 根据 points 总数计算 shm_size
    else 配置文件不存在
        Note over S: max_points = 100000 (默认)
    end
    S->>S: shm_open + ftruncate + mmap<br/>初始化 Header + Data Block Array
    S-->>A: create_shm 调用结果 (point_count, max_points)
```

### 2.2 分配算法

Agent 生成的配置文件中，各 MCP Server 的 `points` 数组中 `shm_id`
均默认为 0（表示尚未分配）。`c4_shm_manager` 在读取配置文件后，按以下算法计算所需空间
并分配全局唯一的 shm_id。

**MCP Server 分类**：

| 角色 | MCP Server | 说明 |
|------|-----------|------|
| **Writer** | `c4_modbus_client`、`c4_iec104_client`、`c4_asfp2_server` | 向共享内存写入数据 |
| **Reader** | `c4_asfp2_client`、`c4_influxdb_client` | 从共享内存读取数据 |

> **注**：`c4_asfp2_server` 的 points 中为 `{addr → shm_id}` 的反向映射（按 ASFP2 协议 key 匹配），
> 与 `c4_modbus_client` 的 `{key → shm_id}` 形式不同，但同样参与 `adjust_shm` 的 shm_id 分配和回填。
> 详细设计见 [c4_asfp2_server.md](c4_asfp2_server.md)。

**算法**：

`c4_shm_manager` 根据配置文件中的 `c4_shm_manager.writer` 字段识别哪些 MCP Server 类型
需要分配 shm_id。该字段由 Agent 生成，数据来源于各 MCP Server 注册时声明的角色属性。

> **前置条件**：以下算法仅在配置文件存在且非空时执行。若配置文件不存在、为 null、
> 或文件内容为空 JSON，c4_shm_manager 创建默认 10 万点共享内存空间（max_points = 100000, point_count = 0），
> 不涉及 shm_id 分配和配置回填。

Writer 端通过 `{service_id}.{point_id}` 组合形成全局唯一 key（如 `hnals_1_scada.windspeed`），
Reader 端通过 `key` 字段引用同一 key。`c4_shm_manager` 据此为同一 data flow 的双方分配相同 shm_id。

```
1. 解析配置文件，读取 c4_shm_manager.writer 和 c4_shm_manager.reader 分类
   ┌ 若 c4_shm_manager 段缺失 → 报错，拒绝创建
   ├ 若 writer 和 reader 均为空 → 视为无配置，按默认 10 万点创建（不分配 shm_id）
   ├ 若仅一方为空（writer 空但 reader 非空，或反之）→ 报错，拒绝创建
   └ 若双方均非空 → 继续
2. 遍历 writer 中列出的每个 MCP Server 类型，统计其 points 数组长度
   → writer_points = Σ 各 Writer 的 points.length
3. 计算共享内存容量：
   max_points = writer_points × 2   // 2 倍余量，供后续扩容
   shm_size = (max_points + 1) × 32  // +1 为 Header
4. 创建并初始化共享内存（shm_open → ftruncate → mmap → 初始化 Header + Data Block）
5. 为每个 Writer point 分配全局 shm_id（从 1 开始连续递增）：
   for each Writer 实例：
     for each entry in points：
       pid = next_id++
       key = "{service_id}.{point_id}"   // point_id 为 point 的 id 字段，如 "windspeed"
       ┌ 若 key 已存在 → 报错（key 冲突，存在重复的 service_id 或 point_id），拒绝创建
       └ 若 key 唯一 → 记录 key → pid 映射
   → 如 `hnals_1_scada.windspeed → 1`, `hnals_1_scada.temperature → 2`, ...
6. 回填 Reader 的 shm_id：
   遍历 reader 中列出的每个 MCP Server 类型，对每个 point entry：
     根据 entry.key 查找步骤 5 记录的 key → pid 映射
     ┌ 若找到映射 → 填入对应的 pid
     └ 若未找到 → 报错（Reader 引用了不存在的 Writer key），拒绝创建共享内存
     （同一 key 可出现在多个 Reader 中，实现一对多数据流）
7. 返回 create_shm 调用结果：{point_count, max_points, 分配后的配置文件}
   （c4_shm_manager 将回填后的配置文件写入磁盘）
```

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant CFG as config.json

    A->>S: create_shm({instance_id})
    S->>A: roots/list
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}

    S->>CFG: 读取配置文件
    Note over S: 遍历 Writer：<br/>modbus#1: [{id:windspeed},{id:temperature}]<br/>modbus#2: [{id:windspeed},{id:temperature}]<br/>iec104#1: [{id:uab},{id:ubc},{id:uac}]<br/>iec104#2: [{id:alarm1},{id:alarm2},{id:alarm3}]<br/>writer_points=10

    S->>S: max_points = 10×2 = 20<br/>shm_open + ftruncate + mmap
    Note over S: 分配 shm_id 并记录 key 映射：<br/>hnals_1_scada.windspeed → 1<br/>hnals_1_scada.temperature → 2<br/>...<br/>hnals_2_transformer.alarm3 → 10

    Note over S: 回填 Reader：<br/>asfp2_client[0]: key=hnals_1_scada.windspeed → shm_id=1<br/>asfp2_client[1]: key=hnals_2_scada.temperature → shm_id=4<br/>...<br/>同一 key 的多个 Reader 填入相同 shm_id

    S-->>A: 分配结果 (point_count=10, max_points=20, 配置文件)
```

**关键性质**：

- **Reader 不参与 shm_id 分配**：Reader 的 `shm_id` 引用 Writer 已分配的值，由 Agent 在 mapping 阶段填入
- **2 倍余量**：50% 的 point 直接在空闲区（state=0），后续新增采集点时若余量足够则无需扩容
- **连续递增**：简化寻址，避免碎片化管理
- **shm_id 从 1 开始**：shm_id=0 表示尚未分配，配置中默认为 0，由 `c4_shm_manager` 分配后回填为 ≥1 的实际值
- **key 解耦位置依赖**：Writer 通过 `{service_id}.{point_id}` 生成全局唯一 key，Reader 通过 `key` 引用。增删条目不影响已有 key 的映射关系
- **一对多数据流**：同一 key 可出现在多个 Reader 中，`c4_shm_manager` 为其填入相同的 shm_id，实现一份数据多路消费

---

## 3. MCP 工具定义

`c4_shm_manager` 通过 MCP 协议向 Agent 暴露以下工具（命名规范：`verb_noun`）。
所有参数和返回值均使用 JSON 格式，schema 遵循 JSON Schema Draft 2020-12。

**MCP 错误约定**：
- **协议层错误**（tool 不存在、参数 schema 不匹配、JSON 解析失败）→ JSON-RPC `error` 对象，code 为 `-32601` / `-32602` / `-32700`，由 MCP 框架/传输层直接返回
- **业务层错误**（tool 成功路由到 handler，但业务逻辑执行失败）→ `result.isError: true`，`content[0].text` 以 `ERROR_TYPE:` 前缀开头

> 示例：参数缺失 `instance_id` → JSON-RPC `{"error": {"code": -32602, "message": "Invalid params: missing required field 'instance_id'"}}`；
> 共享内存已存在 → `{"result": {"content": [{"type": "text", "text": "SHM_ALREADY_EXISTS: ..."}], "isError": true}}`。

---

### 3.1 Tool: `create_shm`

创建共享内存，按 §2.2 算法解析配置文件、分配 shm_id 并初始化共享内存。

**触发条件**：`c4_shm_manager` 首次启动后、Agent 准备启动其他 MCP 服务前调用。

**参数**：

```json
{
    "name": "create_shm",
    "description": "创建 POSIX 共享内存，解析配置文件并按 key 分配全局 shm_id",
    "inputSchema": {
        "type": "object",
        "properties": {
            "instance_id": {
                "type": "string",
                "description": "C4 实例标识符。共享内存命名为 /c4_{instance_id}"
            }
        },
        "required": ["instance_id"]
    }
}
```

**内部流程**：

1. 向 Agent 发送 `roots/list` 请求，获取配置文件绝对路径 URI
2. 若配置文件不存在或为 null（roots/list 返回空、路径无效、或文件内容为空 JSON）：
   a. `shm_open("/c4_{instance_id}", O_CREAT|O_EXCL|O_RDWR)` → `ftruncate` → `mmap`
   b. Header 字段：`magic = 0xC4DA7A00`、`version = 1`、`max_points = 100000`、`point_count = 0`；
      `global_write_seq` 及两个 `reserved` 字段保持 `ftruncate` 零填充后的默认值 `0`（参见 [c4_architecture.md §2.2.1](c4_architecture.md) 初始化规则）
   c. 初始化所有 Data Block 的 `magic = 0xC4DA7A00`（state 自然为 0）
   d. 写入 Header `magic = 0xC4DA7A00`（最终提交）
   e. 跳至步骤 6（不涉及 shm_id 分配和配置回填）
3. 若配置文件存在：
    a. 读取配置文件，判断分类：
       ┌ 若 writer 和 reader 均为空 → 视为无配置，创建默认 10 万点共享内存
       ├ 若仅一方为空（writer 空但 reader 非空，或反之）→ 返回 `CONFIG_MISSING_SECTION` 错误
       └ 若双方均非空 → 按 §2.2 算法分配 shm_id 并回填到各 MCP Server 的 `points` 数组
   b. `shm_open("/c4_{instance_id}", O_CREAT|O_EXCL|O_RDWR)` → `ftruncate` → `mmap`
   c. 初始化所有 Data Block 的 `magic = 0xC4DA7A00`（state 自然为 0）
   d. 写入 Header `magic = 0xC4DA7A00`
   e. 将回填 shm_id 后的配置文件写回磁盘
4. 返回结果给 Agent

**返回值**：成功时返回 `"success"`。
`point_count = 0`、`max_points = 100000` 表示以默认模式创建（配置文件不存在）。

**MCP 应答示例**：

```json
// ========== 成功：配置文件存在，按算法创建 ==========
// --> 请求
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_shm", "arguments": {"instance_id": "hnals_farm_01"}}}
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "success"}], "isError": false}}

// ========== 成功：配置文件不存在，默认 10 万点 ==========
// --> 请求
{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_shm", "arguments": {"instance_id": "hnals_farm_01"}}}
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "success"}], "isError": false}}

// ========== 业务错误：共享内存已存在 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "SHM_ALREADY_EXISTS: /c4_hnals_farm_01 is already created"}], "isError": true}}

// ========== 业务错误：writer 和 reader 仅一方为空 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "CONFIG_MISSING_SECTION: 'c4_shm_manager.writer' is empty but 'c4_shm_manager.reader' is not, both must be non-empty or both empty"}], "isError": true}}

// ========== 业务错误：Writer 端 key 冲突 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "DUPLICATE_KEY: key 'hnals_1_scada.windspeed' already assigned to shm_id=1, duplicate found in c4_modbus_client[1]"}], "isError": true}}

// ========== 业务错误：Reader key 不存在 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "UNKNOWN_READER_KEY: reader 'c4_asfp2_client[0].points[3].key' = 'unknown_scada.windspeed' not found in any writer"}], "isError": true}}

// ========== 业务错误：roots/list MCP 调用失败 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "CONFIG_PATH_MISSING: roots/list protocol call failed, Agent may not be responding"}], "isError": true}}

// ========== 业务错误：系统调用失败 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "SHM_SYSCALL_FAILED: ftruncate failed - ENOSPC (No space left on device)"}], "isError": true}}

// ========== 协议层错误：参数缺失 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid params: missing required field 'instance_id'"}}
```

---

### 3.2 Tool: `adjust_shm`

根据配置文件计算所需点数，判断是否需要扩容或回收，并为所有点分配 shm_id。
已有点的 shm_id 保持不变，新点在空闲块或扩容后的新空间中分配，被删除的点的 block 回收为空闲。

**前置条件**：Agent 必须先通过 Stop-Start 协议暂停所有 MCP 进程的数据路径
（见 §1.2），确认所有进程已暂停后再调用此工具。

**触发条件**：Agent 更新配置文件后（新增/删除 Writer、新增/删除采集点），需要调整共享内存
容量、点分配和块回收。

**参数**：

```json
{
    "name": "adjust_shm",
    "description": "调整共享内存容量和点分配。根据配置文件计算所需点数：回收已删除点的 block，为新点在空闲块中分配，超容量时扩容至 2 倍。已有点地址不变",
    "inputSchema": {
        "type": "object",
        "properties": {},
        "required": []
    }
}
```

**内部流程**：

1. 向 Agent 发送 `roots/list` 请求，获取配置文件绝对路径 URI
   ┌ 若 roots/list 失败或返回空 → 返回 `CONFIG_PATH_MISSING` 错误
   └ 正常 → 继续
 2. 读取配置文件，按 §2.2 算法计算所需点数 (`required_points`)
    ┌ 若 `c4_shm_manager` 段缺失 → 返回 `CONFIG_MISSING_SECTION` 错误
    ├ 若 `c4_shm_manager.writer` 为空（所有 Writer 已删除）：
    │   ┌ 若 reader 也为空 → required_points = 0，回收阶段将所有 state=1 block 置 0（全量回收）
    │   └ 若 reader 非空 → 返回 `CONFIG_MISSING_SECTION` 错误（仅一方为空不合法）
    └ 正常 → 继续
3. 读取当前共享内存 Header，获取 `current_max_points`
4. 容量判断与执行：
   首先执行**块回收**（如果存在孤儿 block）：
      - 遍历配置文件中的所有 Writer point，构建 key（`{service_id}.{point_id}`）→ shm_id 映射
       - 扫描 `block[1..current_max_points]`，找出 `state=1` 的 block
      - 若 block 的 shm_id 在 key→shm_id 映射中**不存在**，则该 block 为孤儿块：
         `state` 置 0（回收），`point_count` 递减
      - 仍存在的 point 对应的 block 不受影响
   然后执行**容量判断**：
    a. **不超容量**（`required_points ≤ current_max_points`）：
      - 扫描 `block[1..current_max_points]`，收集 `state=0` 的空闲 shm_id
      - 为配置文件中 `shm_id=0` 的新点分配空闲 shm_id
      - 已有点（已有 shm_id 的）地址不变
      - `header.point_count = required_points`
   b. **超容量**（`required_points > current_max_points`）：
      - `new_max_points = required_points × 2`
      - `ftruncate(shm_fd, (new_max_points + 1) × 32)`
      - `header.max_points = new_max_points`
      - `munmap` 旧映射 → `mmap` 新大小
      - 新增 block 全部写入 `magic = 0xC4DA7A00`
      - 已有点保持原 shm_id，配置中 `shm_id=0` 的新点分配空闲 shm_id
      - `header.point_count = required_points`
5. 回填配置文件（将分配后的 shm_id 写入各 MCP Server 的 `points` 数组）
6. 写回配置文件到磁盘
7. 返回结果给 Agent

> **部分失败恢复**：`adjust_shm` 的内部操作序列非原子。若中途失败：
> - **写入 Header 后、配置文件写入前失败**：shm 已更新但 config 未回写。此时 `adjust_shm` 的 no-expand 路径通过扫描 `state=0` 分配空闲块，若上次已分配的 block 已是 `state=1`，可能无法完成回填。此场景将在 C4_FUN_00055（回收/再分配）设计中统一处理。
> - **`mmap` 后文件尺寸与 `header.max_points` 不一致**（崩溃发生在 `ftruncate` 后、`header.max_points` 更新前）：重启后 `c4_shm_manager` `mmap` 时检测 `file_size > (header.max_points + 1) × BLOCK_SIZE`，以文件尺寸为准修正 `header.max_points`，然后按 §1.3 崩溃恢复流程重建 `point_count`。
> - **配置文件必须原子写入**：回填配置文件时使用 write-to-temp + `fsync` + `rename` 模式，避免中途崩溃导致配置文件损坏（半写 JSON 无法解析）。

**返回值**：成功时返回 `"success"`。

**MCP 应答示例**：

```json
// ========== 成功 ==========
// --> 请求
{"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "adjust_shm", "arguments": {}}}
// <-- 应答
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "success"}], "isError": false}}

// ========== 业务错误：共享内存未创建 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "SHM_NOT_CREATED: shared memory not initialized, call create_shm first"}], "isError": true}}

// ========== 业务错误：配置文件缺失 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "CONFIG_PATH_MISSING: roots/list protocol call failed, Agent may not be responding"}], "isError": true}}

// ========== 业务错误：c4_shm_manager 段缺失 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "CONFIG_MISSING_SECTION: 'c4_shm_manager' section not found in config"}], "isError": true}}

// ========== 业务错误：系统调用失败 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "SHM_SYSCALL_FAILED: ftruncate failed - ENOSPC (No space left on device)"}], "isError": true}}

// ========== 业务错误：key 冲突 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "DUPLICATE_KEY: key 'hnals_1_scada.windspeed' already assigned to shm_id=1, duplicate found in c4_modbus_client[1]"}], "isError": true}}

// ========== 业务错误：Reader key 不存在 ==========
// <-- 应答
{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "UNKNOWN_READER_KEY: reader 'c4_asfp2_client[0].points[3].key' = 'unknown_scada.windspeed' not found in any writer"}], "isError": true}}
```

---

## 4. 典型交互时序

### 4.1 场景一：首次创建

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant FS as 文件系统

    A->>S: 启动进程
    A->>S: initialize (声明 roots 能力)
    S-->>A: 工具列表

    Note over A: 创建共享内存
    A->>S: tools/call create_shm({instance_id: "my_instance"})
    S->>A: roots/list
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}
    alt 配置文件存在
        S->>FS: 读取 config → 按 §2.2 算法分配 shm_id
    else 配置文件不存在
        Note over S: max_points = 100000 (默认)
    end
    S->>FS: shm_open /c4_my_instance (O_CREAT|O_EXCL)
    S->>FS: ftruncate + mmap + 初始化
    alt 配置文件存在
        S->>FS: 写回 config（回填 shm_id）
    end
    S-->>A: "success"
```

### 4.2 场景二：扩容流程

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant CFG as config.json
    participant M1 as modbus_client
    participant M2 as asfp2_client

    Note over A: 用户新增采集点<br/>Agent 更新配置文件
    A->>CFG: 写入新 points 列表

    Note over A,M2: Phase 1 — Pause
    A->>M1: tools/call stop()
    A->>M2: tools/call stop()
    M1-->>A: "success"
    M2-->>A: "success"
    Note over A: 所有进程已暂停

    Note over A,S: Phase 2 — adjust_shm
    A->>S: tools/call adjust_shm()
    S->>A: roots/list
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}
    S->>CFG: 读取配置，计算 required_points=25
    Note over S: required_points(25) > max_points(20)<br/>new_max = 25×2 = 50
    S->>S: ftruncate + mmap<br/>已有点保持原 shm_id，新点分配<br/>回填配置
    S->>CFG: 写回配置
    S-->>A: "success"

    Note over A,M2: Phase 3 — Start
    A->>M1: tools/call start()
    A->>M2: tools/call start()
    M1->>M1: shm_open + mmap → 重读配置<br/>新 shm_id 加入写入列表
    M2->>M2: shm_open + mmap → 重读配置<br/>新 shm_id 加入读取列表
    M1-->>A: "success"
    M2-->>A: "success"
```

### 4.3 场景三：不扩容（空闲块足够）

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant CFG as config.json
    participant M1 as modbus_client
    participant M2 as asfp2_client

    Note over A: 用户新增少量采集点<br/>Agent 更新配置文件
    A->>CFG: 写入新 points 列表

    Note over A,M2: Phase 1 — Pause
    A->>M1: tools/call stop()
    A->>M2: tools/call stop()
    M1-->>A: "success"
    M2-->>A: "success"

    Note over A,S: Phase 2 — adjust_shm
    A->>S: tools/call adjust_shm()
    S->>A: roots/list
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}
    S->>CFG: 读取配置，计算 required_points=15
    Note over S: required_points(15) ≤ max_points(20)<br/>空闲块足够，不扩容
    S->>S: 扫描空闲块<br/>为新点分配 shm_id<br/>已有点不变
    S->>CFG: 回填配置 → 写回
    S-->>A: "success"

    Note over A,M2: Phase 3 — Start
    A->>M1: tools/call start()
    A->>M2: tools/call start()
    M1-->>A: "success"
    M2-->>A: "success"
```

### 4.4 场景四：Writer 停止后回收

Agent 检测到 writer 心跳超时或收到停止指令后，从配置文件删除对应 writer 的 points，
然后通过 Stop-Start 协议暂停所有 MCP 进程，调用 `adjust_shm` 完成块回收。

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant CFG as config.json
    participant M1 as modbus_client
    participant M2 as asfp2_client

    Note over A: Writer 停止<br/>Agent 从配置删除对应 points
    A->>CFG: 删除 writer3 的 points 列表

    Note over A,M2: Phase 1 — Pause
    A->>M1: tools/call stop()
    A->>M2: tools/call stop()
    M1-->>A: "success"
    M2-->>A: "success"

    Note over A,S: Phase 2 — adjust_shm（回收 + 分配）
    A->>S: tools/call adjust_shm()
    S->>A: roots/list
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}
    S->>CFG: 读取配置，计算 required_points=7
    Note over S: 回收阶段（key 匹配）：<br/>writer3 的 3 个 key 不在配置中<br/>→ block[5..7] state 置 0<br/>point_count: 10→7
    Note over S: 无新增点，分配阶段跳过<br/>required_points(7) ≤ max_points(20)
    S->>S: 回填配置
    S->>CFG: 写回配置
    S-->>A: "success"

    Note over A,M2: Phase 3 — Start
    A->>M1: tools/call start()
    A->>M2: tools/call start()
    M1-->>A: "success"
    M2-->>A: "success"
```

---

### 4.5 场景五：同时回收与再分配（混合操作）

Agent 在一次配置变更中既删除旧 Writer 又新增 Writer，通过 Stop-Start 协议暂停所有 MCP 进程后，
调用 `adjust_shm` 一次性完成回收 → 分配的原子化序列。

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as c4_shm_manager
    participant CFG as config.json
    participant M1 as modbus_client
    participant M2 as asfp2_client

    Note over A: 删除 writer3(3点)<br/>新增 writer5(3点)<br/>Agent 更新配置文件
    A->>CFG: 删除 writer3 的 points<br/>新增 writer5 的 points

    Note over A,M2: Phase 1 — Pause
    A->>M1: tools/call stop()
    A->>M2: tools/call stop()
    M1-->>A: "success"
    M2-->>A: "success"

    Note over A,S: Phase 2 — adjust_shm（先回收，再分配）
    A->>S: tools/call adjust_shm()
    S->>A: roots/list
    A-->>S: {roots: [{uri: "file:///etc/c4/config.json"}]}
    S->>CFG: 读取配置，计算 required_points=10
    Note over S: 回收阶段：<br/>writer3 的 3 个 block → state=0<br/>point_count: 10→7
    Note over S: required_points(10) ≤ max_points(20)<br/>不扩容
    Note over S: 分配阶段：<br/>writer5 的 3 个新点<br/>从回收的 [5..7] 分配<br/>point_count: 7→10
    S->>CFG: 回填配置（writer5 shm_id=5,6,7）
    S-->>A: "success"

    Note over A,M2: Phase 3 — Start
    A->>M1: tools/call start()
    A->>M2: tools/call start()
    M1->>M1: writer3 停止写入<br/>writer5 开始写入 [5..7]
    M2->>M2: writer3 停止读取<br/>writer5 开始读取 [5..7]
    M1-->>A: "success"
    M2-->>A: "success"
```

**关键性质**：

- **回收优先**：回收阶段总是在分配阶段之前执行，无论 required_points 与 point_count 的大小关系
- **等量置换不扩容**：新增点数 ≤ 删除点数 + 原有空闲时，`required_points ≤ max_points`，无需 ftruncate
- **已有点完全不受影响**：writer1、2、4 的 block 保持 state=1，shm_id 不变
- **复用就近原则**：回收产生的空闲 block 在扫描时优先被发现，因此新点倾向于复用刚回收的位置

---

## 5. 错误码汇总

**业务层错误**（`result.isError: true`，`content[0].text` 以 `ERROR_TYPE:` 前缀开头）：

| 错误码 | 含义 | 触发工具 |
|--------|------|---------|
| `SHM_ALREADY_EXISTS` | 共享内存已存在（O_EXCL 冲突） | `create_shm` |
| `SHM_NOT_CREATED` | 共享内存尚未创建 | `adjust_shm` |
| `SHM_SYSCALL_FAILED` | POSIX 系统调用失败（shm_open / ftruncate / mmap） | `create_shm`, `adjust_shm` |
| `CONFIG_MISSING_SECTION` | `c4_shm_manager` 段缺失；或创建时 writer 与 reader 仅一方为空 | `create_shm`, `adjust_shm` |
| `CONFIG_PATH_MISSING` | roots/list MCP 协议调用失败（Agent 不可达或超时） | `create_shm`, `adjust_shm` |
| `DUPLICATE_KEY` | 两个 Writer point 的 `{service_id}.{point_id}` 重复 | `create_shm`, `adjust_shm` |
| `UNKNOWN_READER_KEY` | Reader 的 `key` 字段引用了不存在的 Writer key | `create_shm`, `adjust_shm` |

**协议层错误**（JSON-RPC `error` 对象，由 MCP 框架/传输层直接返回）：

| JSON-RPC code | 含义 | 触发场景 |
|---------------|------|---------|
| `-32601` | Method not found | tool 名称拼写错误或不存在 |
| `-32602` | Invalid params | 必填参数缺失、类型错误、schema 校验失败 |
| `-32700` | Parse error | JSON 格式不合法 |

> **对应功能**：C4_FUN_00053, C4_FUN_00054, C4_FUN_00055, C4_FUN_00056, C4_FUN_00049
