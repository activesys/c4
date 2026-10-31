# C4 资产学习与迁移体系设计

> **版本**：v0.2.2 | **最后更新**：2026-10-29
>
> v0.2.2：第三轮评审残项修订（F1~F6）——折叠触发条件的量词位收窄为编号量词白名单（§7.1）；同站迁移例外同步 §3.1/§9.3；站点层同键同值合并行为定义与迁移/冲突叠加优先级（§7.2）；64KB 外置引用载荷落点（§4.2）；阶段 1 注入排期锚点（§11.3）。
> v0.2.1：第二轮评审残项修订（N0~N9）——同站迁移改只读比较（§7.2）；中文数字折叠限编号语境并进位求值（§7.1）；事件投影与 64KB 上限（§4.2）；条目集收敛/等价投影/负样本证据归 events 层（§3.2/§7.2/§7.3）；LLM 编排层抽取挂 M2（§11.1）；scope 专名回退与全局层 usage_count 初值规则（§3.2/§3.3）；node:sqlite 免 flag 下限以实测为准（§5.1）；联动清单 M1/M2 错位修正（§11.3）；生命周期图首边改「首次命中且确认」（§3.2）。
> v0.2.0：按 2026-10-29 设计评审修订——P0 确认事件采集机制改为逐轮快照+终局比对（§4.1/§4.2）；值条目集模型解决 keep_both 与 UNIQUE 约束矛盾（§7.3）；rejected 复活旁路（§3.2/§7.3）；兼容锚点改 `c4_compat`=packaging/VERSION（§6.1/§6.2）；全局层晋升只落 SQLite、更正 `prompt_hints.protocol_match` 键名（§3.3）；检索器+注入列入 M1 使闭环闭合（§11.1）；events 表补 kind/asset_key/source 列（§5.2）；独立键归一化规范 alias_key_norm（§7.1）；stages/ 两步抽取与打包脚本适配入 M1（§2.1/§2.2/§11.1）；SQLite 驱动选 node:sqlite（§5.1）；value 等价判定定义（§6.3/§7.2）；scope 逐 kind 判定表（§3.3）；回归两级分级（§7.4/§10）；承接 C4_FUN_00093（§11.1）；指标埋点（§10）；trainer 断点续跑（§8.6）；L3 失败语义与成熟度口径（§3.3）。
> v0.1.2：按 2026-10-29 用户裁定明确「沉淀门控以计数为准」（§3.2/§7.2/§7.6/§9.8/§11.2）——不设跨站来源数判定，仅 active 资产可导出。
> v0.1.1：按 2026-10-29 用户裁定新增「场站绑定与场站资产分离」语义（§3.1/§7.2/§11.2）。

---

# 第一章 设计背景与目标

## 1.1 问题定义

C4 接入流程的输入是开放集合的自然语言：点表的格式与表头命名、设备名称的写法、
寄存器地址的形式（进制、起始约定、字序）、网络信息的表述均千变万化。当前
Agent 采用「确定性快路优先（正则/硬编码词表），LLM 阶段提取兜底」的分层识别
（agent.md §1.4、§2.2），能保证流程正确，但识别知识是**静态的**——同类错误
会反复发生，用户纠正不产生积累，每个新站点都从零磨合。

## 1.2 三条正确性不变量

对「绝对正确」的目标作如下重构——识别层对开放输入不可能 100% 正确，系统保证
的是三件可保证的事：

1. **永不静默出错**：识别要么正确、要么明确提问、要么给出候选供选择；
2. **错误必被拦截**：错误结构化输出活不过校验链（agent.md §2.7 的 L0/L1/L2 +
   方案展示 + 按钮确认 + 事务回滚）；
3. **同样的错误不再犯第二次**：确认/修正沉淀为资产，识别随使用演进。

本设计只新增第 3 条；前两条是既有架构的不变量，**确认按钮是永久不变量**——
学习减少的是到达可确认方案之前的追问轮数与中途纠错次数，而不是取消最终确认。

## 1.3 设计目标

- **确认驱动的资产积累**：部署初期为「出厂知识 + 零学习资产」，随使用按阶段
  积累资产，常见接入收敛为一两轮对话直达确认按钮；
- **越用越顺手可度量**：每次成功接入的平均追问轮数、中途纠错次数、LLM 调用
  次数单调下降，一次通过率上升（第十章指标体系）；
- **资产可迁移**：学习资产可导出为版本化资产包，导入其他 C4 实例自动合并，
  单站经验转化为装机群体能力；
- **训练与积累分离**：提供独立训练客户端批量生产资产，快速完成冷启动。

## 1.4 与既有设计的关系

| 既有文档 | 关系 |
|----------|------|
| `agent.md` §2.2 阶段定义表 | 资产按阶段编号段（1、1.5、2~7）归属；本章资产类型表与阶段定义对齐 |
| `agent.md` §2.7 校验分层 | 校验链在 L0/L1/L2 基础上扩展 L3 实证校验（连接探测/寄存器读回） |
| `agent.md` §2.13 点表解析与归一化 | 结构签名缓存挂接在两遍法识别（identify/transcribe）之上；点表域经验学习承接 C4_FUN_00093（§11.1） |
| `c4_architecture.md` | Agent 进程模型、配置体系不变；资产库为 Agent 侧新增共享子系统 |

# 第二章 总体架构

## 2.1 两条架构原则

**原则一：一套学习引擎，九类资产分治。** 学习闭环的机制统一（采集→入库→检索→
注入→晋升→回归），但资产 schema、检索器、校验器按阶段独立——协议阶段永远
检索不到点表模板，修正事件按阶段归因，资产按阶段独立版本化。分治不等于九套
子系统：以共享的 `StageLearner` 接口承载，每个阶段实现一份资产 schema
（第四章）。

**原则二：训练端与运行端共享同一条识别流水线。** 训练客户端若单独实现提取
逻辑，训练产物与运行期使用的语义必然漂移。因此把九阶段提取/校验层从
`orchestrator.ts` 抽出为共享包 `c4/stages/`，trainer 与 agent 共同依赖。抽取
**分两步走**：先抽确定性层（point_pipeline / protocol_attribution /
gap_answer / site_check / zh_numeral / point_key——已具独立模块与依赖注入
先例，如 point_pipeline 的 LLM 回调注入、protocol_attribution 的
ProtocolMatcher 闭包注入，低风险）；再以注入接口（`LlmJson` / `Logger` /
`RegistryData`）抽 LLM 编排层——`run_stage_extraction` 约 1800 行内联、深度
耦合会话状态与 `llm_json` 闭包，须先定义边界与函数签名，**不是低风险搬运**。
资产入库走同一接口与同一 SQLite schema——「训练积累」与「使用积累」是同一种
数据，导入合并只是数量叠加，不涉及格式转换。

## 2.2 组件与代码布局

```
c4/
├── stages/               # 新增 npm workspace 共享包
│   └── src/              #   九阶段提取/校验层（自 orchestrator.ts 两步抽出，§2.1）
│                         #   + StageLearner 接口 + 资产 kind 的 Zod schema
├── assets/               # 新增 npm workspace 共享包
│   └── src/              #   StageLearner 实现 + SQLite 资产库 + 合并引擎
│                         #   （agent 与 trainer 同为消费方，避免反向依赖 agent/src）
├── agent/
│   └── src/server/routes/assets.ts   # 新增：导入导出 REST
├── trainer/              # 新增：训练客户端（Express + Web，复用 stages/ 与 assets/）
├── docs/design/c4_asset_learning.md   # 本文档
└── packaging、ChangeLog   # 各期随代码变更递增版本号（文档变更不递增）
```

Agent 由单包改为 npm workspaces 的**同时必须适配打包链**：`packaging/build.sh`
现为 `cp -a agent/node_modules` + `npm prune --omit=dev` 的单包布局，workspaces
提升后依赖上移 `c4/node_modules`，该流程需重写；prompts 以 `import.meta.url`
相对定位（`render_prompt`），stages/ 包自带提示词后拷贝与解析路径需重排。
打包脚本适配列为 **M1 显式工作项**（§11.1）。

**不引入** PostgreSQL/pgvector/Langfuse（工业现场单机部署，SQLite + 现有
NDJSON 日志足够）；**不迁移** LangGraph（orchestrator 手写状态机即「外层显式
状态机」，保持审计刚性）。

## 2.3 架构与数据流

```
┌────────────────────────────────────────────┐
│  训练客户端 c4/trainer/（独立进程，办公网）     │
│  语料目录批量摄入 → 九阶段流水线（共享 stages/）│
│  → 置信分级 → 批量审核 → 资产沉淀（共享 assets/）│
│  → 自动回归 → 导出 .c4pack                    │
└───────────────┬────────────────────────────┘
                │  .c4pack.json（版本化资产包）
┌───────────────▼────────────────────────────┐
│  C4 Agent 导入/导出 + 合并引擎                │
│  REST /api/assets/import|export + CLI       │
│  全局层计数沿用 / 站点层试用期 / 冲突队列       │
│  回归前置验证 / 事件审计可回滚                 │
└───────────────┬────────────────────────────┘
                │
┌───────────────▼────────────────────────────┐
│  运行端九阶段 StageLearner                   │
│  SQLite 资产库（~/.local/c4/assets.db）      │
│  运行期确认/修正事件 → 事件入库 → 候选资产      │
│  检索 → prompt 注入 / 快路词表 → 晋升          │
└────────────────────────────────────────────┘
```

完整生命周期：**出厂（registry 内置知识）→ 部署站点（可选导入历史资产包作
冷启动种子，站点层进试用期）→ 冷启动期（高频确认，事件带阶段标签入库）→
积累期（别名/模板/约定沉淀转正，热路径命中，追问轮数下降）→ 熟练期（常见接入
一两轮直达确认按钮）→ 导出资产包（仅 active）→ 新部署导入种子，跳过大半个
冷启动**。

# 第三章 资产模型

## 3.1 三层资产

| 层 | 内容 | 信任语义 | 迁移语义 |
|----|------|----------|----------|
| **出厂层** | registry JSON（协议别名、点字段枚举映射、`point_normalization`、类型同义词、表头别名） | 随产品发布，第 0 天可信 | 随安装包 |
| **全局层** | 跨站点成立的模式（协议别名「部颁=CDT」、类型同义词「浮点=float」、措辞语料「都是1=全部同值」） | 计数达标沉淀转正（§3.2），可进确定性快路 | **可直接导入生效**（携带计数，达标恢复 active） |
| **站点层** | 设备别名、结构签名→列映射模板、地址约定（如 modbus 40001=1 起）、站点默认端口惯例 | 仅对本站可信 | **导入后进试用期**（首次命中仍走确认）；同站迁移例外见 §7.2 |

站点层资产**永远不能免确认直接信任**：「1#主变」在 A 厂的指称对 B 厂无效。
全局层与站点层的判定标准：该模式是否绑定站点上下文——绑定即站点层。

**场站绑定与场站资产是两个概念**（2026-10-29 用户裁定）：场站绑定（`agent.json`
`site` 字段）是本实例的身份基准，只经显式绑定/改名通道（site_config）变更，
**资产导入永不触碰**。站点层资产是 `origin_site` 标记的**先验知识**，只用于
比较与提取辅助——例如本实例绑定为华能阿拉善，导入华能通辽、国电山西的资产
包后，用户输入中出现与通辽/山西表述习惯相符的内容时，检索命中这些资产可提升
提取正确率（场站别名类资产同时辅助归属判定：帮助判别输入内容**不属于**本站），
但**本站归属不因此变为通辽或山西**。站点层资产经试用期在本站被连续确认后，
获得的是「本站已验证的先验」地位（active），`origin_site` 标记保留不变，不
发生归属转移。场站绑定也不是站点层资产的前置条件：未绑定时导入的种子包照常
入库并进入试用流程——绑定影响的是阶段 1 归属判定基准（site_check），不影响
资产存储与试用。

## 3.2 资产生命周期

```
candidate（候选：确认事件生成 / 导入生成）
   │  运行期首次命中且被用户确认（进入试用期）
   ▼
probation（试用期：可被检索注入，但命中结果仍走确认）
   │  连续确认达标（站点层 confirm_streak ≥ 3；全局层 usage_count ≥ 5）
   ▼
active（转正/沉淀：进确定性快路 / 免检索注入直接命中）
   │  任一次命中被用户修正
   ▼
rejected（打回：记负样本，计数清零；可复活，见旁路）
```

- `confirm_streak` 连续确认计数：命中且确认 +1，命中且修正清零并记负样本；
- `usage_count` 计数：本机新学 candidate 首次确认记 1，此后命中且确认 +1；
  导入资产携带包内计数，本机确认继续累加；
- **计数是沉淀的门控**（2026-10-29 用户裁定）：资产在库内以 `candidate` 聚合计数，
  达到计数标准才沉淀转正——全局层 `usage_count ≥ 5`，站点层试用期
  `confirm_streak ≥ 3`。沉淀后计数使命完成，资产即可被导出；未达标的
  candidate 仅供检索注入，不导出；
- 导入的全局层资产携带包内 `usage_count`：已达标者直接恢复 `active`（全局
  语义跨站成立），未达标者保持 `candidate` 由本机确认继续累计；
- **rejected 复活旁路**：rejected 条目以最低优先级参与检索且命中强制走确认
  （§7.3）；同键再次产生确认事件时生成新 candidate 条目——避免「不检索则
  永无命中、无命中则永不复活」的自锁。rejected 条目不参与常规排序，负样本
  证据由 events 层承载（§4.2）；
- 资产行不物理删除；append-only 语义由事件审计层保证（§7.5）。

## 3.3 分阶段资产类型表

阶段编号沿用 agent.md §2.2（5~7 为 2~4 的转发侧实例，共享同一资产域）。

| 阶段段 | 领域 | 表面变化维度 | 资产类型 | 校验链 | 晋升产物 |
|--------|------|--------------|----------|--------|----------|
| 1 | 场站信息 | 场站称谓、口语指代 | 场站别名对（站点层） | 绑定唯一性 | 场站别名表 |
| 1.5 | 设备信息 | 设备命名体系（1#主变 / 一号主变 / INV-07 / T1） | 设备别名对、命名模式签名（前缀+数字风格+后缀）、撞名负样本（站点层） | 注册表唯一性、候选选择题 | 设备别名表、命名模式 |
| 2/5 | 接入/转发协议 | 协议别名与口语（104 / 南104 / 485 / 部颁 / CDT） | 协议别名对（**全局层为主**）、误判负样本 | 封闭枚举四态 + 证据子句（已有）；溯源校验 | SQLite 全局层协议别名表，运行期注入 `prompt_hints.protocol_match` 拼装处与快路词表（**不写回 registry 文件**，§7.7） |
| 3/6 | 接入/转发点表 | 文件结构（表头/合并单元格/分段行）、点字段语义（寄存器类型/功能码/字序）、地址形式（进制/起始/序列描述） | 结构签名→列映射模板、表头别名增量、类型同义词增量、地址约定画像、陷阱列负样本（站点层为主） | **签名命中抽样复核**（地址列单调性 first_drop、类型列枚举符合率）、枚举/跨度/功能码校验（已有）、溯源校验、L3 读回校验（远期） | 模板库 + 词表三层化 + 站点地址约定 |
| 4/7 | 接入/转发信息 | IP/端口/参数写法 | 站点默认端口惯例、org/bucket 命名习惯（站点层）；案例记忆 | 格式校验、溯源校验、**L3 连接实证探测**（远期） | 站点画像 |
| 横切 | 缺口应答 | 「都是1 / 全一样 / 同上 / 照旧」 | 槽位措辞案例（短语→绑定动作对） | 槽位封闭域枚举/范围校验（已有） | 案例库（动态 few-shot） |
| 横切 | 意图词形 | 「转发到influx / 送库 / 入库」 | 意图短语→意图标签对（全局层可积累） | 目标必须命中注册表（选择题） | 快路正则语料扩展 |
| 8 | 方案层 | —（纯代码，无学习） | — | — | — |
| 9 | 执行层 | —（确定性拆解器） | — | — | — |

**scope 判定规则**（逐 kind，`onConfirm` 确定性执行）：

| kind | scope | 判别条件 |
|------|-------|----------|
| protocol_alias / type_synonym / phrase_corpus / intent_phrase | global | 语义不含场站专名；含专名（命中场站绑定名/别名词表或 site_alias 资产）→ 回退 site |
| header_alias | global 或 site | 通用表头词 → global；含场站/系统专名 → site |
| device_alias / column_mapping_template / address_convention / site_alias / default_port / storage_naming | site | 一律站点层 |

**L3 实证校验失败语义**（远期，两处口径一致）：确认前连接探测/寄存器读回失败
时，方案层标注「未通过实证校验」并展示失败详情，**确认按钮维持武装、用户显式
确认可继续**——fail-visible，不静默阻断也不静默放行。

# 第四章 学习事件与 StageLearner

## 4.1 确认事件采集

用户的改口发生在**提取回合的合并期**（agent.md §2.3「单调累积 + 显式改口才
覆盖」），到达 `handle_confirm` 时会话草稿已包含全部用户修正——「确认时刻草稿
vs 提取产物」的差异主要是系统派生（channel 序号分配、点 key 前缀改写、类型
补齐、reader 点集重排），**不能作为修正信号**。采集机制为：

1. **逐轮快照**：每个提取回合，各阶段把 LLM 原始提取输出（proposed 快照）写入
   会话级暂存缓冲（session-scoped，随草稿生命周期）；
2. **终局比对**：确认成功时（`handle_confirm`，草稿清空之前），以「该键任意
   一轮提取值 ≠ 最终值」判定 corrected——用户在多轮对话中任何时点的改口均被
   捕获；
3. **系统派生排除**：比对前按 kind 剔除系统派生字段清单（channel、前缀、
   序号等），仅对用户语义字段计 corrected。

采集分支约定：

- **内嵌 JSON 直通确认**（`handle_confirm` 内嵌路径，无阶段提取产物）：不产生
  corrected 样本，事件记 `source=embedded`（仅作接入事实记录，不入学习聚合）；
- **会话草稿为纯内存（LRU 32）**：重启/淘汰即丢失在途快照——该确认不产生学习
  事件（可接受取舍，显式写明）；已落库事件不受影响（落库发生在确认时刻）。

## 4.2 事件记录

```typescript
interface ConfirmEvent {
  stage: StageTag;          // 阶段编号段：'1' | '1.5' | '2' | '3' | '4' | 'gap_answer' | ...
  session_id: string;
  input_ref: string;        // 用户原文片段（≤4KB 截断）或文件引用 file:<path>#rows<a-b>
  proposed: unknown;        // 提取结果
  final: unknown;           // 确认/修正后结果
  corrected: boolean;       // 是否被修正（§4.1 机制判定）
  confidence: number;       // 提取时置信度
  source: 'stage' | 'embedded';
  occurred_at: string;
}
```

事件 append-only，永不修改；资产是事件的聚合视图——`onConfirm` 由 StageLearner
写入 `kind` 与 `asset_key`（归一化键，§7.1），按键聚合更新 `confirm_streak`
（§3.2），事件可完整回放重建资产。

**事件体量政策**：`proposed` / `final` 按 kind 做**投影**——只存资产相关判定
结果（如阶段 3 只存列映射决策与结构签名，不存整表数据）；单事件 `proposed` /
`final` 各上限 64KB，超限存摘要 + 外置引用（`file:<path>#rows<a-b>`）——防止
阶段 3 整表事件（可达 5 万点）使 assets.db 无界膨胀。外置引用的载荷文件由
StageLearner 写入 `~/.local/c4/asset_payload/`（按事件 id 命名，覆盖粘贴文本
与文件两种来源的超限场景）；资产包导出时打包为包内 `payload/` 目录，导入侧
校验引用的载荷文件存在，缺失则该事件按摘要入库并告警。

## 4.3 StageLearner 接口

```typescript
// c4/stages/src/learner.ts —— 接口定义；实现在 c4/assets/ 共享包
interface StageAsset {
  kind: AssetKind;          // 'protocol_alias' | 'device_alias' | 'column_mapping_template' | ...
  stage: StageTag;          // 归属阶段段（§3.3）
  scope: 'global' | 'site'; // 按 §3.3 scope 判定规则确定性归属
  key: string;              // 归一化键（alias_key_norm，§7.1）
  value: unknown;           // 结构由 kind 的 Zod schema 约束；可歧义 kind 为值条目集（§7.3）
  status: 'candidate' | 'probation' | 'active' | 'rejected';
  usage_count: number;
  confirm_streak: number;
  origin_site?: string;     // 站点层必填；检索降权依据（仅溯源，不参与计数，§9.8）
  version: number;
}

interface StageLearner {
  /** 确认/修正 → 事件入库 + 候选资产生成/更新 */
  onConfirm(event: ConfirmEvent): Promise<void>;
  /** 该阶段专属检索：只返回本阶段资产，注入 prompt 或词表快路 */
  retrieve(query: RetrieveQuery): Promise<StageAsset[]>;
  /** 阶段校验链：文法 → 语义 → 溯源 →（远期）L3 实证 */
  validate<T>(output: T, ctx: ValidationContext): ValidationResult;
  /** 置信门槛评估：晋升 / 打回 / 生成待人工裁决项 */
  promote(): Promise<PromoteReport>;
}
```

## 4.4 失败归因到层

修正事件必须带阶段标签，修复才能沉淀为正确类型的资产：协议误判 → 协议别名；
列映射错误 → 表头别名或陷阱负样本；地址歧义 → 站点地址约定画像。**禁止跨阶段
兜底归因**（如把列映射修正记给协议阶段）。

# 第五章 资产库存储

## 5.1 存储选型

SQLite 单文件（`~/.local/c4/assets.db`），驱动选 **`node:sqlite`**（Node 内置
模块，零新依赖、纯 JS 打包链不受影响；22.5 引入但早期版本需
`--experimental-sqlite` 显式开启，**免 flag 可用的版本下限以打包预置 Node
二进制实测为准**，不按 22.5 选型；API 处于实验期，Node 升级须回归）。与现有
`config.json`/`abbr_registry.json` 同级。检索以键匹配 + 确定性相似度为主
（复用 §3.6.4 suggest 的编辑距离 + 公共子串算法）；**embedding 通道推迟**——
等确定性手段的召回缺口实测出来再引入（届时选 ONNX 本地小模型，如
bge-small-zh-v1.5，模型 id 一经使用即锁定并写入资产包 `embedding` 字段作兼容性
锚点，换模型须全量重建向量）。

## 5.2 表结构

```sql
CREATE TABLE assets (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,             -- AssetKind
  stage TEXT NOT NULL,            -- StageTag
  scope TEXT NOT NULL CHECK (scope IN ('global','site')),
  key TEXT NOT NULL,              -- alias_key_norm 归一化键（§7.1）
  value TEXT NOT NULL,            -- JSON；可歧义 kind 为值条目集（§7.3）
  status TEXT NOT NULL DEFAULT 'candidate',
  usage_count INTEGER NOT NULL DEFAULT 0,
  confirm_streak INTEGER NOT NULL DEFAULT 0,
  origin_site TEXT,               -- 站点层必填；仅溯源与降权，不参与计数（§9.8）
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (kind, stage, scope, key)   -- 同键单行；多值以值条目集建模（§7.3）
);

CREATE TABLE events (            -- append-only 确认事件（资产聚合的事实源）
  id INTEGER PRIMARY KEY,
  stage TEXT NOT NULL, session_id TEXT NOT NULL,
  kind TEXT,                      -- 资产 kind（onConfirm 由 StageLearner 写入）
  asset_key TEXT,                 -- alias_key_norm 键（同上）
  source TEXT NOT NULL DEFAULT 'stage',   -- stage | embedded（§4.1）
  input_ref TEXT NOT NULL,        -- ≤4KB 截断或 file:<path>#rows<a-b> 引用
  proposed TEXT NOT NULL, final TEXT NOT NULL,
  corrected INTEGER NOT NULL, confidence REAL NOT NULL,
  occurred_at TEXT NOT NULL
);

CREATE TABLE conflicts (         -- 导入冲突队列（§7.3）
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, stage TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
  existing_value TEXT NOT NULL, incoming_value TEXT NOT NULL,
  pack_id INTEGER NOT NULL,      -- 来源导入批次
  resolution TEXT,               -- NULL=未裁决 | 'keep_existing' | 'accept_incoming' | 'keep_both'
  created_at TEXT, resolved_at TEXT
);

CREATE TABLE regression_cases (  -- 回归用例（随包导入 + 运行期沉淀）
  id INTEGER PRIMARY KEY,
  stage TEXT NOT NULL, input TEXT NOT NULL, expect TEXT NOT NULL,
  level TEXT NOT NULL DEFAULT 'deterministic',  -- deterministic | e2e（§7.4）
  origin TEXT NOT NULL,          -- 'pack:<id>' | 'runtime'
  created_at TEXT NOT NULL
);

CREATE TABLE pack_imports (      -- 导入审计与回滚点
  id INTEGER PRIMARY KEY,
  pack_file TEXT NOT NULL, pack_version TEXT NOT NULL, checksum TEXT NOT NULL,
  imported_at TEXT NOT NULL, rollback_snapshot TEXT NOT NULL,  -- 导入前 assets 快照文件
  status TEXT NOT NULL DEFAULT 'applied'   -- applied | rolled_back
);
```

# 第六章 资产包格式（.c4pack.json）

## 6.1 包结构

```jsonc
{
  "format": "c4-asset-pack",
  "pack_version": "1.0",            // 包格式版本
  "asset_schema_version": "1.0",    // 资产 schema 版本（kind 的 Zod schema 集与
                                    //   alias_key_norm 规则版本，§7.1）
  "c4_compat": "2.1.103",           // 锚定 c4/packaging/VERSION（打包唯一权威版本源）
  "embedding": null,                // 嵌入模型兼容性锚点（未引入为 null；引入后锁 id）
  "source": { "site": "example_fd", "created_at": "2026-10-29T00:00:00Z",
              "generator": "trainer" | "agent-export" },
  "assets": {
    "global": {
      "protocol_aliases":  [ { "alias": "部颁", "canonical": "cdt", "usage_count": 12 } ],
      "type_synonyms":     [],
      "header_aliases":    [],
      "phrase_corpus":     [ { "phrase": "都是1", "stage": "gap_answer", "action": "uid_all" } ],
      "negative_samples":  []
    },
    "site_layer": [
      { "kind": "column_mapping_template", "stage": "3", "key": "<结构签名>",
        "value": { "mapping": { "addr": "信息体地址", "name": "点名称" } },
        "origin_site": "example_fd", "usage_count": 12, "status": "active" },
      { "kind": "device_alias", "stage": "1.5", "key": "一号主变",
        "value": { "canonical": "1#主变" }, "origin_site": "example_fd" },
      { "kind": "address_convention", "stage": "3", "key": "modbus_base",
        "value": { "modbus_40001_base": 1 }, "origin_site": "example_fd" }
    ]
  },
  "regression": [
    { "stage": "2", "level": "deterministic", "input": "部颁CDT转发",
      "expect": { "protocol": "cdt" } }
  ],
  "checksum": "sha256:..."          // 对 assets+regression 规范化 JSON 计算（§6.3）
}
```

## 6.2 版本兼容规则

导入器校验，任一不满足则拒收并报告：

1. `format` 必须为 `c4-asset-pack`；
2. `asset_schema_version` ≤ 导入器支持的资产 schema 版本（向后兼容，缺 kind 按
   unknown kind 跳过并告警，不整包拒收）；
3. `c4_compat` 与本机 `c4/packaging/VERSION` 同 MINOR 系——packaging/VERSION
   是 c4 打包唯一权威版本源，registry 知识随包分发，以其为锚无需 registry 侧
   新增版本字段；跨 MINOR 导入须人工确认并跑全量回归；
4. `embedding` 非空时必须与本机锁定的嵌入模型 id 完全一致，否则含向量资产
   全部拒收（防止相似度空间漂移）。

## 6.3 完整性

**规范化 JSON** 定义为：键递归排序、无空白、UTF-8 编码的序列化结果——`checksum`
（SHA-256）与 §7.2 的 value 等价判定共用同一实现。导入前验签，不一致整包拒收。

# 第七章 导入、合并与导出

## 7.1 键归一化（alias_key_norm）

资产键**不直接复用** `normalize_point_name`——该函数只做 NFC、全角折叠、白名单
符号、小写（point_key.ts），**不折叠中文数字**，「一号主变」与「1#主变」归一化
后仍是两个键。资产键定义独立规范：

```
alias_key_norm = normalize_point_name 折叠
               + 编号语境中文数字折叠
               + 分隔符等价类（# / 号 等价，- / _ 等价）
```

使「一号主变」≡「1#主变」≡「1号主变」，不产生重复资产。

**中文数字折叠只在编号语境触发**——`zh_numeral.ts` 是「从 X 开始/起」句式的
起始地址提取器，不是通用折叠工具，逐字替换会把「十一号」错折、把「一致」
「统一」「二次回路」等普通词误折成数字键。规则：(a) 触发条件为数字串紧邻
数字/`#`/**编号量词白名单成员**（封闭清单：号、号机、号机组、台等；「次」
「回」等通用量词不在清单——「二次回路」不触发折叠），或整串为纯中文数字；
(b) 组合数字按进位求值（十一=11、
一百二十=120），不逐字替换；(c) 其余语境一律不折叠。alias_key_norm 的
规则版本随 `asset_schema_version` 记录，规则变更须全量重算现存键。

## 7.2 合并规则

value 等价判定 = **规范化 JSON 深比较**（§6.3 同一实现）；`usage_count` /
`confirm_streak` 不参与等价比较。值条目集形态（§7.3）的等价判定 = 先投影为
裸值集合（剥离各条目的 `usage_count` / `confirm_streak` / `origin_site`）
再做规范化比较，且与条目顺序无关。

| 情形 | 规则 |
|------|------|
| 全局层新键 | 直入为 `candidate` 并携带包内 `usage_count`：已达标（≥5）直接恢复 `active`，未达标由本机确认继续累计（§3.2） |
| 全局层同键同值（value 等价） | `usage_count` 取 max 合并，去重跳过 |
| 全局层同键冲突值 | **不覆盖**，双值写入冲突队列（§7.3），检索降权直至裁决 |
| 站点层新键 | 一律 `candidate` → 运行期试用期（confirm_streak≥3 转正）；带 `origin_site` 检索降权 |
| 站点层同键且 value 等价（投影等价，本节首段） | **existing status 保留**（本站已验证资产不打回试用期）；计数取 max 合并；不产生新条目、不进冲突队列 |
| 站点层同键冲突值 | 进冲突队列；`origin_site` 相同（同站迁移）时以 incoming 为候选、existing 保留待裁决 |
| 回归用例 | 合入回归集并标 `origin=pack:<id>`，先跑后激活（§7.4） |

导入任何资产都**不修改**本实例的场站绑定（`site` 字段，§3.1 分离原则）；
「同站迁移」分支指 `origin_site` 与本站绑定一致的情形（整实例迁移/重装）——
该判定**只读比较**（读 `site` 与包内 `origin_site` 比对，不写回）：一致时
站点层资产按备份恢复语义处理，保留资产包内记录的 status（已 active 的不重进
试用期）。同键冲突不适用备份恢复语义，仍进冲突队列（§7.3）——恢复语义与
冲突裁决叠加时，冲突裁决优先。

## 7.3 冲突裁决与值条目集

同键唯一行约束不变（§5.2），多值以**值条目集**建模：可歧义 kind 的 `value` 为
JSON 数组，每条目含 `value` / `usage_count` / `confirm_streak` / `origin_site`。

- **keep_both**：incoming 作为新条目追加进条目集；检索返回全部条目并按
  `usage_count` 降权排序；**裁决前默认返回 existing 条目**；
- 命中条目被确认 → 该条目 streak+1；达沉淀标准 → 条目集收敛为该单条（歧义
  消解，仅移除非胜出的 candidate 条目）；命中条目被修正 → 该条目标记
  rejected——不参与常规排序、仅以最低优先级参与复活检索（§3.2），负样本证据
  由 events 层承载（条目集可安全清理 rejected 条目）；
- rejected 条目以最低优先级参与检索且命中强制走确认；同键新确认事件生成新
  candidate 条目（复活旁路，§3.2）。

裁决入口：M2 提供 CLI（`c4-agent assets conflicts`：列表 + 逐条
keep_existing / accept_incoming / keep_both）；Web 裁决页放 M4。

## 7.4 回归前置（两级分级）

回归用例分两级：**确定性级**（快路词表 / 归一化 / 签名匹配——不调 LLM，门禁
必跑，要求 100% 通过）与 **LLM 端到端级**（完整流水线——LLM 非确定性会抖动，
抽样异步执行、输出容差报告，**不作为门禁**）。`level` 列区分（§5.2）。

导入流程：验签 → 版本校验（§6.2）→ 合并（事务内）→ 确定性级回归全量 +
端到端级抽样 → 门禁全绿则提交并写 `pack_imports` 审计；门禁失败则回滚事务、
整包不激活、输出差异报告。回归执行走共享 stages/ 流水线（与运行期同源）。

## 7.5 版本化与回滚

- assets 行**原地更新 + version 递增**，状态迁移同步写 events / 审计——
  append-only 语义由事件审计层保证（events 可回放重建资产），非行级拷贝
  （行级拷贝与同键唯一约束矛盾）；
- 每次导入在 `pack_imports.rollback_snapshot` 存导入前 `assets` 表快照
  （SQLite backup 至 `~/.local/c4/snapshots/pack-<id>.db`），
  `c4-agent assets rollback <pack_id>` 一键回滚；
- 运行期确认产生的资产变更按天滚转快照（`snapshots/assets-YYYYMMDD.db`，
  保留 30 天）。

## 7.6 接入入口

| 入口 | 场景 |
|------|------|
| `POST /api/assets/import`（multipart 上传 .c4pack.json） | Web 人工导入，返回合并报告 + 冲突清单 |
| `GET /api/assets/export?scope=global|all&sanitize=bool` | Web 人工导出 |
| `c4-agent assets import|export|conflicts|rollback|status`（CLI） | 自动化部署/无人值守 |
| trainer 内导出 | 训练客户端直接产出资产包（不经过 agent） |

**导出内容门控**（2026-10-29 用户裁定）：仅导出已达沉淀标准（`active`）的资产
——`candidate` / `probation` / `rejected` 属本地过程数据，不进资产包；
`usage_count` 随包携带，供导入侧沿用计数（§3.2）。

## 7.7 全局层晋升产物只落 SQLite

全局层资产（如协议别名）**只存 SQLite 并由检索器在运行期注入**——注入点为
`render_prompt` 的 registry hint 拼装处与快路正则的运行期词表叠加。**不写回
registry 文件**：registry 是随安装包分发的产品文件，运行期写回会被升级覆盖，
且违反三层模型（出厂层只随产品发布）。registry 侧既有知识键名以实际结构为准
（`prompt_hints.protocol_match` 等，loader.ts 四节结构）。

# 第八章 训练客户端（c4/trainer）

## 8.1 定位与部署

独立进程（Express + Web，与 agent 同栈），部署在办公网/开发机，连接与生产
相同的 GLM 端点；**不部署在工业服务器**。定位：批量生产资产、快速完成新站点
或新装机群体的冷启动，产物即 .c4pack。

## 8.2 批量训练工作流

```
指定语料目录（点表 xlsx/csv / 需求描述 txt/md / 历史会话 NDJSON）
  → 逐文件/逐会话跑九阶段流水线（共享 stages/）
  → 置信分级：过硬校验全过且 confidence ≥ 阈值 → 自动入库
              其余 → 批量审核队列
  → 批量审核（Web 表格网格：原文片段｜识别结果｜修正列；
               支持整批采纳 / 单行修正）
  → 确认事件走与运行端相同的 StageLearner.onConfirm 入库
  → 批后自动跑全量回归集 + 指标报告
  → 一键导出 .c4pack
```

## 8.3 批量审核

表格网格一行一个候选，比聊天逐条确认快一个数量级；支持按阶段过滤、按置信度
排序、批量「全部采纳」、单行修正（修正即产生 `corrected=true` 事件）。审核
UI 放 M4，M3 以最小 Web 列表 + CLI 交互替代。

## 8.4 自动入库阈值

默认 **0（全人工审核）**。跑顺后按阶段放开（如协议别名 0.9），阈值按阶段独立
配置。放开条件：该阶段回归用例 ≥ 50 条且近 30 天一次通过率 ≥ 95%。

## 8.5 种子语料

仅使用 c4 仓库内语料（2026-10-29 用户裁定：**不使用** `tests/scenarios` 与
`/home/wangbo/backup/test/`）：

- `c4/test/func_case_e2e/points/1#风机点表.xlsx|.csv`
- `c4/test/web/e2e/fixtures/1#|2#风机点表.csv`
- `c4/test/` 各功能用例的会话 fixtures（后续随用例积累扩充）

## 8.6 CLI 与自动化

```
trainer batch <dir> [--auto-threshold 0] [--stage 2,3] [--out pack.c4pack.json]
trainer review            # 启动审核 UI
trainer export [--sanitize] [--scope global|all]
trainer metrics           # 批后指标报告
```

无人值守模式（threshold>0 + 回归全绿）可整批自动产出资产包，人工只看报告。
**断点续跑**：batch 维护批处理清单（`<dir>/.trainer-manifest.json`，逐文件状态
pending / done / failed / reviewed），重启后续跑，不重复已完成文件。

# 第九章 安全与防污染规则

1. **只从确认/修正事件学习**：被拒绝的方案仅记负例信号，不产生正样本资产；
2. **高置信自动入库、低置信人工确认**：自动入库阈值默认 0（§8.4）；
3. **站点隔离**：站点层资产不跨站自动生效，导入必经试用期（同站迁移例外见
   §7.2）；场站绑定
   与场站资产分离，导入不触碰身份（§3.1）；
4. **冲突上报**：同键异值不覆盖，进冲突队列等人工裁决（§7.3）；
5. **回归保护**：资产变更（导入/晋升/词表更新）前先跑确定性级回归（§7.4），
   防止「学了新表、忘了旧表」；回归用例随真实确认持续沉淀（`origin=runtime`）；
6. **资产版本化可回滚**（§7.5）；
7. **确认按钮永久不变量**：任何资产状态都不绕过方案确认与事务（§1.2）；
8. **晋升门槛（2026-10-29 用户裁定：以计数为唯一沉淀门控）**：站点层
   `confirm_streak ≥ 3` 且期间无修正；全局层 `usage_count ≥ 5`。**不设跨站
   来源数判定**——`origin_site` 只是溯源元数据（单值、参与检索降权），跨站
   来源不做计数依据，因此无需来源集合结构。全局层误沉淀由运行期兜底：active
   资产被用户修正即打回 `rejected` 并记负样本（§3.2）。

# 第十章 演进指标

| 指标 | 定义 | 目标趋势 |
|------|------|----------|
| 结构签名命中率 | 点表文件走签名缓存零 LLM 的占比 | 上升 |
| 词表覆盖率 | 各阶段快路词表命中、免 LLM 的提取占比 | 上升 |
| 一次通过率 | 方案首次展示即被确认（无修正）的占比 | 上升 |
| 平均追问轮数 | 每次成功接入的单缺口提问轮数 | 下降 |
| 中途纠错次数 | 每次成功接入的 corrected=true 事件数 | 下降 |
| 会话 LLM 调用次数 | 每次成功接入的阶段提取 LLM 调用数 | 下降 |
| 回归通过率 | **确定性级**回归全量通过（变更门禁） | 恒为 100% |

**指标数据源与埋点**：现有 NDJSON 事件（user_input / llm_call / assistant_text
等）可直接算「会话 LLM 调用数」与「追问轮数（启发式）」；「词表覆盖率 /
一次通过率 / 结构签名命中率」需随 **M1 补埋点**——`llm_call` 事件增加 `stage`
标签、新增 `plan_shown` / `confirm` 结构化事件，指标口径以事件定义为准；含
LLM 的端到端指标走抽样统计（§7.4 第二级），不进门禁。

# 第十一章 分期实施计划

## 11.1 里程碑

| 里程碑 | 交付物 | 验收标准 |
|--------|--------|----------|
| **M1 共享层与运行端闭环** | ① `c4/stages/` 两步抽取之第一步：确定性层（point_pipeline / protocol_attribution / gap_answer / site_check / zh_numeral / point_key 迁入）；② `c4/assets/` 共享包（SQLite 资产库 + StageLearner 实现）；③ 协议别名（阶段 2/5）**检索器 + prompt/词表注入 + 采集**（样板闭环：命中→确认→confirm_streak 递增）；④ 打包脚本适配（build.sh workspaces 布局 + prompts 路径）；⑤ 指标埋点（llm_call stage 标签、plan_shown/confirm 事件） | agent 构建与全部功能测试通过；协议别名「命中→确认→计数→candidate 生长」闭环可观测；打包产物可安装 |
| **M2 导入导出与合并** | ① `c4/stages/` 第二步：LLM 编排层抽取（LlmJson / Logger / RegistryData 注入接口，§2.1）；② 合并引擎（值条目集 + 冲突队列 + 试用期状态机）+ 回归两级门禁 + REST/CLI + 回滚快照 | 资产包导入→合并→回归→审计全链路可走通；冲突不静默；回滚可执行；trainer 所需的 LLM 阶段流水线就绪 |
| **M3 trainer 骨架** | CLI 批量跑流水线（含断点续跑）+ 最小审核 + 打包导出；用 §8.5 种子语料产出首个资产包 | 种子点表批量产出资产包，导入另一 C4 实例生效 |
| **M4 体验完善** | 批量审核 Web UI、指标报告、冲突裁决页、脱敏导出 | §8.2 全工作流 UI 可操作 |

M1 交付物中检索器+注入是**闭环成立的前提**——没有命中就没有 confirm_streak，
采集与晋升机制将空转，故与采集同里程碑交付。LLM 编排层抽取（§2.1 第二步）
排在 M1 确定性层验证后、M2 交付物①内，作为独立提交分批迁移——M3 trainer 的
九阶段流水线（含 LLM 阶段）依赖它。

**功能编号**：点表域经验学习**承接 C4_FUN_00093**（agent.md §2.13.9 已登记的
识别经验学习；C4_FUN_00092 方案期模板兜底维持既有机制，本体系的结构签名模板
库作为其数据来源之一，二者不合并）；资产库 / 导入导出 / 训练客户端的其余功能
条目在 c4_function.md 立项时分配新编号。

每期随代码变更递增 `c4/packaging/VERSION` PATCH 位并写 ChangeLog（文档先行
不递增）。

## 11.2 已定决策记录

| 决策 | 结论 | 日期 |
|------|------|------|
| 训练端与运行端关系 | 共享 stages/ 识别流水线与资产 schema，资产同构入库 | 2026-10-29 |
| trainer 位置 | c4 仓库内独立目录（`c4/trainer/`），npm workspaces 共享 | 2026-10-29 |
| 合并语义 | 全局层计数沿用/去重/冲突队列；站点层一律试用期 N=3 | 2026-10-29 |
| 自动入库阈值 | 默认 0（全人工），放开条件见 §8.4 | 2026-10-29 |
| 冲突裁决入口 | M2 CLI 先行，Web 裁决页放 M4 | 2026-10-29 |
| 种子语料范围 | 仅 c4 仓库内语料；**不使用** `tests/scenarios` 与 `/home/wangbo/backup/test/`（用户裁定） | 2026-10-29 |
| 嵌入检索 | 推迟引入；确定性相似度先行；引入后模型 id 锁定为资产兼容性锚点 | 2026-10-29 |
| 存储选型 | SQLite 单文件（node:sqlite 驱动）；不引入 PostgreSQL/pgvector/Langfuse | 2026-10-29 |
| 编排框架 | 不迁移 LangGraph，维持 orchestrator 手写状态机 | 2026-10-29 |
| 设备白名单废除、设备资产沉淀（2026-10-31 用户裁定） | 演进的 C4 最初不知道任何设备——未知设备不拒绝：缺口询问 → 用户确认 → 接入 → 沉淀设备资产（阶段 1.5 站点层试用期 streak≥3 转正，随 M1 落地）；10-27 设备白名单裁定废止（全新实例空注册表无法引导首台设备，10-29 e2e 决定性对照实测） | 2026-10-31 |
| 场站绑定与场站资产分离（2026-10-29 用户裁定） | 场站绑定是身份基准，导入资产永不触碰；站点层资产只是 origin_site 标记的比较与提取辅助先验——绑定为阿拉善、导入通辽/山西资产包，命中只提升对通辽/山西风格内容的提取，本站不变成通辽或山西；active 后 origin_site 保留，无归属转移；未绑定也可导入，照常试用 | 2026-10-29 |
| 沉淀门控以计数为准（2026-10-29 用户裁定） | 计数达标才沉淀成资产、才能导出：全局层 usage_count≥5、站点层试用期 confirm_streak≥3；**不设跨站来源数判定**（origin_site 仅溯源元数据，无需来源集合结构）；导入的全局资产携带包内计数，达标恢复 active；candidate/probation/rejected 不导出 | 2026-10-29 |

## 11.3 文档联动清单

本文档描述的是**待实施**子系统（M1~M4），现有文档描述当前实现状态，均不失真；
各文档在对应里程碑落地时必须同步更新，作为该里程碑验收的一部分：

| 里程碑 | 联动文档 | 更新内容 |
|--------|----------|----------|
| 立项（M1 开工前） | `docs/specification/c4_function.md` | 资产库 / 导入导出 / 训练客户端功能条目与编号分配（点表域承接 C4_FUN_00093）；`c4_requirement.md` 评估是否新增需求条目 |
| M1 | `docs/design/agent.md` | §5 文件结构补 `c4/stages/`、`c4/assets/` 共享包（替换「未单设 stages/ 目录」注记）；§2.7 校验分层补 L3 实证校验（待实施）；§6 设计决策记录补资产体系行。根 `AGENTS.md` 构建说明补 workspaces；`packaging/build.sh` 适配 |
| M2 | `docs/design/web.md`、`docs/design/agent.md` | web.md §1.3 后端能力盘点补 `/api/assets/import`、`/api/assets/export` 行（待实施标注随实现移除）；agent.md §5 补 `server/routes/assets.ts`；§3.2.1.3a 补场站别名资产辅助归属判定的语义扩展（阶段 1 资产注入在 M1~M4
范围外，随后续批次排期，落地当期联动） |
| M3 | `docs/design/c4_deployment.md` | 补训练客户端（办公网部署、GLM 端点依赖）说明；`README.md` 组件清单补 `trainer/`；`c4_function.md` 功能条目标 ✅ 并补测试套件目录（`c4/test/`） |
| M4 | `docs/design/web.md`、`c4_deployment.md` | 批量审核 UI / 冲突裁决页 / 脱敏导出对应的前后端设计补齐 |

超出本表范围的既有文档（`c4_shm_manager.md`、各 MCP 服务设计文档、`asfp2_specification.md`、
`c4_description.md`）不受本体系影响。
