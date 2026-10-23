# Medical Agent Assistant 开发任务计划

版本：v2.0，2026-10-16。增量状态更新：2026-10-23。

本次已接通可持久化的手工规则工作流：查房文本／浏览器转写 → 病历及来源保存 → 四类候选小任务 → 医生确认与排期 → 结果／执行记录 → 查看确认 → 处理结论 → 未完成事项交班草稿。病历详情按 ID 请求后端，支持刷新读回；查房提交可幂等重试。涉及 T01、T05、T07、T12–T15 的部分交付。

26 项后端测试和独立数据库的浏览器完整链路验证已通过。已新增明确的结束查房操作（COMPLETED）、查房历史与后续任务计数；执行型任务可凭医生实际执行记录完成，检验型任务继续分步审核，待办与完成记录分开查看。当前 PostgreSQL 已通过可回滚事务验证，既有数据保留。自动报告匹配与 Finding 审核、常驻 Worker 运维、真实模型、两次事件恢复／动态重规划、正式身份认证及交班快照确认仍未完成，不将本次交付标为完整 M1/M2 达标。已补齐同一患者多次查房、按患者／住院查看历史病历与原始查房、独立的新一次查房入口，历史正文和任务不覆盖。下方完整任务的未完成框保留各自整体验收口径。

目标是在 `F:/health_agent` 现有医生工作台和 FastAPI/PostgreSQL 后端上，实现一个能记住临床随访目标、等待外部事件、恢复调查、核对证据、交由医生审核并生成交班草稿的系统。保留查房与病历辅助功能，新增临床工作流连续性能力。

本文作为后续开发主清单。此前 `docs/plans/MVP_TASKS.md` 是规则闭环的初步拆分；本版结合两份新增参考材料，补齐独立的 Intent/Open Loop、Workflow Memory、动态取证、暂停恢复和重规划验收。以下是完整规划；当前实现以文首增量状态和已勾选项为准，不能把其余计划视为已有功能。

## 1 参考依据与采用方式

参考材料：

- 用户提供的《ClinLoop 华为 ICT 大赛系统设计文档 v0.1》：产品目标、Agent 运行机制、状态与证据设计、医生端五个视图。
- `2026-09-28-clinloop-mvp-implementation-plan.md`：任务分解、工具与服务边界、回放评测和交付步骤。
- 本项目 `index.html`、`backend/main.py`、`backend/requirements.txt`：当前实现与迁移起点。
- 本地 ClinLoop 的 `agent.py`、`planner.py`、`guard.py`、`server.py`：抽样检查实现，帮助区分设计与实际代码。

本地 ClinLoop 副本与先前提供的线上 README 不是已确认相同版本。抽样代码中，planner 主要按事件类型返回固定步骤，Agent 主要生成运行记录，工具注册也不等于已提供 MCP 协议服务。因此不能把目录名、方法名或运行轨迹当成动态 Agent 已实现的证明；本项目需用真实工具执行与分支测试验收。

本地副本 `pyproject.toml` 的许可字段为 `Proprietary`，根目录未见 LICENSE。本计划参考架构并独立实现；若后续决定直接复制代码，应先确认授权与署名要求。此次不复制对方源码，不运行其重置、部署或 Git 指令。

## 2 当前基础及本次采用的实现路线

| 当前文件或能力 | 继续使用 | 本期处理 |
| --- | --- | --- |
| `index.html` | 医生工作台视觉、患者列表、病历交互 | 分离 JS/CSS，改为后端驱动；增加任务、证据和审核面板 |
| `backend/main.py` | FastAPI 入口与既有病历 API | 拆分模块，保持 `/api/medical-records` 兼容 |
| `doctors/patients/medical_records` | 开发数据库中的现有数据 | 引入 Alembic，增加 encounter 与工作流表，不清空原数据 |
| PostgreSQL 连接配置 | 用户自己的开发数据库 | 新增专用测试库配置和健康检查 |
| 模拟病程、提醒、交班 | 演示素材 | 替换为真实保存、查询与生成；仍可保留明确的离线演示模式 |

默认采用 Python 3.12+、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL 和一个 Python Worker。先以 PostgreSQL 待处理队列实现持久化事件分发；前端先拆分现有原型，不把 React 或 Redis 迁移作为必做前置。平台阶段如需 React、MCP、AgentArts，再在独立任务中接入。

### 三个交付层次

| 层次 | 交付 | 关键区别 |
| --- | --- | --- |
| M1 基础闭环 MVP | 查房 → 候选任务 → 报告 → 证据 → 医生确认 → 交班 | 规则驱动，可重复运行；如实标为规则版 |
| M2 Agent MVP | 加入真实模型、工具选择、Workflow Memory、两次恢复和一次重规划 | 必须有实际调用和根据返回结果改变计划的证据 |
| M3 比赛扩展 | 更多事件/缺口、真实 MCP/平台适配、扩展评测 | 分别验收；不将离线样本或适配器存在等同于在线支持 |

### MVP 主剧情

```text
09:10 查房：血培养结果出来后通知我
  → 提出 FOLLOW_RESULT 候选意图；医生确认
  → 创建血培养 Open Loop，保存 waiting_for，暂停
14:30 血培养结果返回
  → 恢复同一意图，查询报告与后续记录
  → 无明确确认记录，形成待审核 Finding，转医生审核
18:00 医生确认：已查看，继续等待药敏
  → 保存结果确认；医生确认后关闭当前跟进环节
  → 在同一意图下创建药敏依赖 Loop，重规划并再次暂停
19:00 药敏结果到达
  → 第二次事件恢复，核对报告与确认记录；未确认则保留待审核
20:00 开始交班
  → 将仍待处理的药敏 Loop 纳入交班草稿，医生确认快照
```

18:00 的关闭、后续任务确认均为医生明确操作。接受提醒、查看报告和完成处理分别记录，不能隐式连带关闭任务。

## 3 全局契约

### 对象和状态

- `ClinicalIntent` 是医生提出的目标，保留原文、来源、患者、住院和类别。
- `OpenLoop` 是完成该目标所需持续跟进的环节；一个 Intent 可有多个依赖 Loop。前端“临床任务”是 OpenLoop 的展示名称，不再额外建一套同义 `clinical_tasks` 表。
- `Evidence` 引用原始事件/记录的指定版本，记录来源、时间和核验状态。
- `Finding` 是待审核的问题主张，必须带证据和检索范围；不是自动形成的临床结论。
- `Review`、`Acknowledgement`、`Resolution` 分别表示提醒审核、结果查看确认和任务处理完成。
- `AgentRun` 表示一次调查；重试 attempt、跨事件恢复和 plan 版本分别记录。

```text
PROPOSED → 医生确认 → WAITING_EVENT
WAITING_EVENT → 已核验报告 → RESULT_AVAILABLE
RESULT_AVAILABLE → 医生明确确认 → ACKNOWLEDGED
ACKNOWLEDGED → 医生记录处理结论 → RESOLVED
PROPOSED → REJECTED
未关闭环节 → 医生带理由操作 → CANCELLED
```

逾期、证据冲突和数据过期作为独立标记；队列失败、运行超时作为运行状态，不覆盖业务状态。没有报告证据不能进入 RESULT_AVAILABLE；不提供任意 PATCH 状态接口。

### 数据与执行约束

- 证据、任务、事件和操作始终校验患者与此次住院；姓名和床号不能充当关联键。
- 检验匹配优先使用医嘱/标本/报告编号和版本；同项目多次采样不能靠名称猜测。
- 时间带时区，保存发生时间与接收时间；相对日期以原始记录时间为基准。
- “未找到”必须带来源、时间窗与查询是否成功；查询失败不能当作无记录。
- 规则或模型只提交候选；状态服务负责权限、证据、版本、医生操作和审计。
- 工具默认只读，医生写操作通过受控业务 API。临床原文、报告和工具返回均按数据处理，不可改变工具权限或系统规则。
- 重复投递允许发生，业务效果必须幂等；不承诺消息只投递一次。

## 4 按层次划分任务

整个项目可以按从底到顶的七层来理解。下层提供数据和规则，上层提供 Agent 能力和医生使用界面。

```text
第 7 层：比赛扩展与发布
  T18 扩展事件和缺口 · T19 MCP/AgentArts · T20 文档/CI/交付

第 6 层：评测与运行安全
  T16 回放评测 · T17 故障恢复、预算、并发和边界测试

第 5 层：医生工作台
  T13 Timeline / Open Loops / Workflow Gaps / Evidence / Trace / Handoff

第 4 层：业务 API 与临床服务
  T05 查房/事件接口 · T11 Verifier/Guard · T12 审核/确认/审计
  T14 病历衔接 · T15 交班草稿

第 3 层：事件与 Agent 运行层
  T06 只读工具 · T07 意图提取 · T08 Worker · T09 Memory/暂停恢复 · T10 动态 Agent

第 2 层：领域模型与状态规则
  T03 Event/Intent/OpenLoop/Evidence/状态机契约

第 1 层：基础设施与数据基础
  T01 前后端连接 · T02 模块化/迁移 · T04 工作流表/住院范围/种子
```

### 第 1 层：基础设施与数据基础

任务：**T01、T02、T04**。

这一层解决“系统能不能稳定保存和读取数据”。先修复当前病历页面的联机行为，再把后端拆分并引入数据库迁移，最后增加住院、事件、任务和证据所需的表。

完成这一层后，你应当能够确认：

- 病历从 PostgreSQL 读取，修改后可以刷新读回；
- 数据库结构有迁移文件，不依赖启动时临时建表；
- 同一患者的不同住院可以区分；
- 合成数据可以重复导入，不会产生重复记录。

### 第 2 层：领域模型与状态规则

任务：**T03**，并与 **T11、T12** 共同完成状态约束。

这一层定义系统到底保存什么、状态如何变化。核心对象是：

```text
Clinical Intent  医生提出的临床意图
Open Loop        尚未完成的持续跟进环节
Evidence         支持判断的原始证据
Finding          等待医生审核的问题提醒
Review           医生审核记录
Audit            不可覆盖的操作记录
```

这一层完成后，系统必须能判断：

- “结果返回”不能直接变成“任务已闭环”；
- 医生接受提醒不等于医生已经查看结果；
- 医生查看结果不等于已经完成处理；
- 没有有效证据不能推进任务状态；
- 模型不能直接写入最终状态。

### 第 3 层：事件与 Agent 运行层

任务：**T06、T07、T08、T09、T10**。

这是项目的智能部分，但应建立在第 1、2 层之上：

1. T06 提供安全的只读工具，让 Worker 能查询当前住院的事件、检验和病历。
2. T07 从查房文本中提取候选随访意图，并由医生确认。
3. T08 让事件进入可恢复的后台 Worker。
4. T09 保存 Workflow Memory，使任务能够暂停、被事件唤醒并恢复。
5. T10 才接入真实模型，让 Agent 根据工具返回结果改变下一步计划。

这一层的完成标志不是“模型生成了一段文字”，而是：同一个 Intent 能经历至少两次事件唤醒，并且第二次调查会根据新证据重新规划。

### 第 4 层：业务 API 与临床服务

任务：**T05、T11、T12、T14、T15**。

这一层把底层数据和 Agent 能力变成可调用的医院业务流程：

- T05：保存查房、病历和模拟检验事件；
- T11：匹配检验报告、绑定证据、发现流程缺口；
- T12：医生接受/驳回提醒、确认结果、记录处理并审计；
- T14：将查房来源、临床任务和病历草稿关联起来；
- T15：根据仍未闭环的任务生成交班草稿。

这一层完成后，接口应能串起：

```text
查房保存
→ 候选意图确认
→ 检验事件到达
→ 证据匹配
→ Finding 审核
→ 结果确认/任务处理
→ 交班草稿
```

### 第 5 层：医生工作台

任务：**T13**。

医生端不以聊天框为中心，而以患者工作流为中心，建议按五个区域实现：

| 视图 | 解决的问题 |
| --- | --- |
| Patient Timeline | 最近发生了什么 |
| Open Loops | 还有什么事情没有完成 |
| Workflow Gaps | 哪个环节可能断了 |
| Evidence Drawer | 提醒依据来自哪里 |
| Agent Trace / Handoff | Agent 做了什么，以及如何交班 |

T13 可以在 T03 之后先做静态骨架，但所有状态、任务和证据必须以第 4 层 API 返回的数据为准。

### 第 6 层：评测与运行安全

任务：**T16、T17**。

这一层回答两个问题：

- 系统是否找对了任务和流程缺口？
- 出错、超时、重复提交或并发修改时是否安全？

必须评测错患者、错住院、报告修订、重复事件、Worker 重启、工具失败和医生审核边界。规则、mock 和真实模型分开统计，不能用固定预期结果代替 Agent 运行效果。

### 第 7 层：比赛扩展与发布

任务：**T18、T19、T20**。

这层不应阻塞基础 MVP：

- T18 扩展到更多事件和流程缺口；
- T19 接入 MCP 和 AgentArts 运行时；
- T20 完成演示、CI、版本说明和发布检查。

只有第 1–6 层的本地闭环稳定后，才开始平台适配。平台接入不能替代本地状态、证据和审核逻辑。

### 分层执行顺序

```text
基础数据层
  → 领域规则层
  → 事件/Agent 层
  → 业务 API 层
  → 医生工作台
  → 评测与安全
  → 比赛扩展与发布
```

允许并行的部分：T13 的页面骨架可以在 T03 后开始；T16 的测试数据和指标定义可以在 T03 后准备。不能提前依赖的部分：T10 真实 Agent 依赖 T06、T08、T09、T11；T15 交班依赖任务和证据真实落库；T19 平台接入依赖本地 Agent 链路通过验收。

## 5 任务总览

| 层 | 编号 | 任务 | 依赖 | 交付层次 |
| --- | --- | --- | --- | --- |
| 1 基础数据 | T01 | 修复原型数据连接与保存反馈 | 无 | M1 |
| 1 基础数据 | T02 | 后端模块化与数据库迁移 | T01 | M1 |
| 2 领域规则 | T03 | 冻结事件、意图、Loop 和状态契约 | T02 | M1 |
| 1 基础数据 | T04 | 工作流持久化、住院范围与合成种子 | T03 | M1 |
| 4 业务 API | T05 | 查房、病历和事件录入 API | T04 | M1 |
| 3 Agent 运行 | T06 | 只读证据工具与白名单 | T04 | M1 |
| 3 Agent 运行 | T07 | 意图提取与医生确认 | T05、T06 | M1 |
| 3 Agent 运行 | T08 | 可靠 Worker 与事件路由 | T05、T07 | M1 |
| 3 Agent 运行 | T09 | Workflow Memory 和暂停恢复 | T06、T08 | M1/M2 |
| 3 Agent 运行 | T10 | 真实 Agent 与动态重规划 | T09、T11、T12 | M2 |
| 4 业务 API | T11 | 报告匹配、Verifier 和 Guard | T06、T09 | M1 |
| 4 业务 API | T12 | 医生审核、结果确认与审计 | T11 | M1 |
| 5 医生工作台 | T13 | 医生端五个工作流视图 | T03 可起骨架，联调依赖 T12 | M1/M2 |
| 4 业务 API | T14 | 查房与病历草稿关联 | T05、T12、T13 | M1 |
| 4 业务 API | T15 | 交班草稿与不可覆盖快照 | T12、T13 | M1 |
| 6 评测安全 | T16 | 回归、基线和事件回放评测 | M1 主链路；模型评测依赖 T10 | M1/M2 |
| 6 评测安全 | T17 | 恢复、预算、并发写与边界测试 | T08–T16 | M1/M2 |
| 7 扩展发布 | T18 | 扩展六类事件和四类流程缺口 | M2 验收 | M3，可选 |
| 7 扩展发布 | T19 | MCP 协议与 AgentArts 运行时适配 | T06、T10、T17 | M3，按比赛需要 |
| 7 扩展发布 | T20 | 文档、演示、CI 与版本交付 | 所选层次任务验收通过 | 各层次 |

## 6 逐项开发任务

上传评审前已复核勾选：分页、报告版本、完整工具查询范围与截断语义、事件匹配、延迟重试、跨事件恢复和不可覆盖的编辑版本仍未整体验收，保持未完成。已有部分代码不等于整项完成。

以下路径均相对于本仓库；新文件是计划，不表示文件已存在。每项交付同时包含相关测试，T16 负责统一评测，不把所有测试推迟到最后。

### T01 修复现有原型数据连接与保存反馈

涉及文件：修改 `index.html`；新增 `frontend/api.js`、`frontend/app.js`、`frontend/styles.css`、`tests/frontend/records.spec.js`。

- [ ] 抽出页面脚本、样式和统一 API client，保留原视觉布局。
- [ ] 统一病历 DTO，修复搜索中仍使用旧字段、创建时间冒充签署时间的问题。
- [x] 首次进入病历模块就请求列表；空列表显示空态。
- [x] 明确演示/联机模式，联机错误显示重试，不静默回退到本地写入。
- [ ] 动态文本使用安全 DOM 渲染，去除患者正文拼入 HTML/inline handler 的路径。
- [x] 查房保存接口未实现前标明未保存，不用成功提示冒充持久化。

验收：实际修改病历并刷新可读回；断网、空库有不同反馈；切换患者不显示前一个患者数据；原文中的 HTML 作为文本显示。

### T02 后端模块化与 Alembic 迁移

涉及文件：修改 `backend/main.py`、`backend/requirements.txt`；新增 `backend/config.py`、`backend/db.py`、`backend/models/legacy.py`、`backend/routes/records.py`、`backend/schemas/records.py`、`alembic.ini`、`backend/migrations/`、`tests/db/test_legacy_migration.py`。

- [x] 拆分现有 ORM、请求模型和路由；保持原病历 API 兼容。
- [ ] 新增依赖管理、测试配置和后端静态检查配置。
- [x] 空库用迁移建表，已有库先比对实际结构，在副本完成 baseline 和升级验证。
- [ ] 处理旧无时区时间：确认历史值约定后显式转换，不按本机时区猜测。
- [x] 引入数据库 readiness，保留服务存活接口；移除依赖启动时 create_all 升级结构的假设。

验收：旧记录 ID、正文和关联保留；原查询/编辑接口回归通过；迁移失败回滚，不执行删除用户数据库的命令。

### T03 定义共享契约与状态转换

涉及文件：新增 `backend/contracts/{enums,events,workflow,api}.py`、`backend/domain/{state_machine,errors}.py`、`tests/domain/test_state_machine.py`、`docs/architecture/contracts.md`。

- [x] 定义 ClinicalEvent、ClinicalIntent、OpenLoop、Evidence、Finding、AgentRun、Review 和 Handoff DTO。
- [ ] M1 支持 NOTE_CREATED、LAB_RESULT_CREATED、PROGRESS_NOTE_CREATED、HANDOFF_STARTED 四类事件；确认操作产生独立结构化审计/事件。
- [ ] 冻结 FOLLOW_RESULT 与结果未确认提醒；未知意图返回待确认/不支持。
- [x] 输入拒绝未知字段，ID 非空，时间带时区，置信度在 0–1。
- [x] 实现纯函数状态转换规则，不在状态机里直接写库。
- [ ] 冻结错误码、状态版本字段和前端 DTO 映射。

验收：不能跳过医生操作将 RESULT_AVAILABLE 改为 RESOLVED；仅接受 Finding 不会改变确认/闭环状态。

### T04 工作流表 住院范围和合成数据

涉及文件：新增 `backend/models/workflow.py`、`backend/repositories/`、`backend/dependencies.py`、`backend/routes/patients.py`、迁移、`fixtures/{cases,events}.json`、`scripts/seed_demo.py`、`tests/db/test_workflow_schema.py`。

- [x] 新增 encounters、doctor_encounter_assignments、rounding_sessions、clinical_events、event_jobs。
- [x] 新增 clinical_intents、open_loops、evidence_nodes、findings。
- [x] 新增 agent_runs、agent_steps、review_decisions、result_acknowledgements、handoff_reports、audit_logs。
- [ ] 核心对象保存 patient_id、encounter_id、来源 ID、时间与版本；设置必要外键和幂等唯一约束。
- [ ] Workflow Graph 先用关系表表示，不引入图数据库；依赖不能形成环。
- [ ] 开发模式显式选择模拟医生，服务端读取分配范围；不信任客户端传入的医生身份。
- [x] 种子含两个医生、三个患者、一个患者两次住院；固定业务 ID，重复执行不重复插入，禁止清空非演示数据。

验收：旧病历完成 encounter 映射；其他住院和未授权患者访问被拒绝；同一患者不同报告版本可区分；种子可重复导入。

### T05 查房 病历和事件录入接口

涉及文件：新增 `backend/routes/{rounding,events,timeline}.py`、`backend/services/event_service.py`、`tests/api/test_event_ingestion.py`；修改病历路由。

- [x] 实现 `POST /api/rounding-sessions`、`POST /api/medical-records`，真正保存查房和草稿。
- [x] 开发模式提供 `POST /api/events` 导入模拟报告，关闭开发模式则不可用。
- [x] 原始记录、事件和待处理项在同一事务内写入。
- [x] 幂等键相同且内容一致时返回已有结果；键相同但内容不同返回 409。
- [ ] 区分 event_time、received_at、report_version，返回可追踪 job_id。
- [ ] 提供按住院查询时间线和任务/处理状态的 API；分页并固定排序。

验收：保存后刷新能看到查房；失败事务不留下孤立事件；病历和工作流均可回到来源会话。

### T06 只读工具和证据查询服务

涉及文件：新增 `backend/tools/{registry,schemas,repository_tools}.py`、`tests/tools/test_read_tools.py`。

- [ ] 提供 get_patient_snapshot、get_recent_events、get_labs、get_progress_notes、get_handoff、get_loop_state。
- [ ] 每个工具验证 actor、patient_id、encounter_id 和时间范围，统一返回来源引用及查询是否完整。
- [ ] 结果区分 found、empty、partial、error；记录请求范围和报告版本。
- [x] ToolRegistry 拒绝未注册工具、任意 SQL 和任意文件访问，限制每次查询条数。
- [x] 医生审核、任务关闭和交班确认不注册为模型可直接调用的写工具。

验收：调用读工具不改变数据库；工具错误不会被 Verifier 视为“没有记录”；非法患者范围在工具层同样拒绝。

### T07 规则提取候选意图及医生确认

涉及文件：新增 `backend/services/intent_service.py`、`backend/providers/{base,rules}.py`、`backend/routes/intents.py`、`tests/intent/test_follow_result.py`。

- [x] 识别“血培养结果出来后通知我”，保留原文片段与检验目标。
- [x] 区分否定、历史事实、条件计划和明确新意图；未明确日期保持 null。
- [ ] 相对日期以事件发生时间解析，不能以后台处理日期替代。
- [x] 医生确认候选意图后创建 OpenLoop；可修改不确定字段或带理由驳回。
- [x] 为同一事件内的候选建立稳定指纹，防止处理重试重复创建。
- [ ] 等待药敏建为同一意图下的依赖环节，保留父 Loop 和来源医生操作。

验收：规则模式无需模型密钥；“无需复查”“昨日已经查看结果”不直接新建已确认任务；候选、确认与等待状态明确。

### T08 数据库队列 Worker 与事件路由

涉及文件：新增 `backend/worker/{main,queue,router,processor}.py`、`tests/worker/test_queue_recovery.py`。

- [x] Worker 领取 event_jobs，保存 lease、attempt、retry_at、last_error。
- [ ] 成功提交业务结果后再完成 job；异常有限重试，超限转可见失败状态。
- [ ] 按住院、事件类型、目标和 waiting_for 找候选 Loop，再由匹配服务复核。
- [ ] 事件投递和业务效果分别幂等；重试可以增加 attempt，不重复生成 Finding。
- [ ] 每个已提交事件对应可观测处理结果，不能因 Redis 不可用而静默丢到进程内存。
- [ ] 单 Worker 起步，医生与 Worker 同时修改同一 Loop 时使用乐观锁或事务锁。

验收：领取后崩溃可在租约到期恢复；重启不丢已确认任务；重复消费不重复提交；失败原因可查询。

### T09 Workflow Memory 和暂停恢复

涉及文件：新增 `backend/worker/{memory,run_repository}.py`、`backend/services/loop_service.py`、`tests/worker/test_suspend_resume.py`。

- [ ] 持久化 intent_id、loop_id、waiting_for、dependencies、last_plan、next_check 和状态版本。
- [ ] 每次恢复只加载当前住院、相关 Loop、相关报告和必要记录；限定数量与时间窗。
- [x] 原始证据独立保存，模型摘要不能替代原始来源。
- [ ] 新 run 记录明确的 prior_run_id、触发事件及计划版本，不能把自身 ID 当作恢复来源。
- [ ] 不持续轮询所有病历；事件路由唤醒，定时任务只处理到期复核项。
- [ ] 记录 WAITING_EXTERNAL_EVENT、REQUIRES_CLINICIAN_REVIEW、TOOL_ERROR 等明确停止原因。

验收：两次不同结果事件都能恢复正确 Loop；重启后 intent 不变、run 可串联；报告先到时能回查；无关事件不唤醒其他患者任务。

### T10 真实模型 Agent 动态调查与重规划

涉及文件：新增 `backend/worker/{agent,planner,runner}.py`、`backend/providers/{llm,mock}.py`、`tests/agent/test_adaptive_tools.py`、`tests/agent/test_replanning.py`；修改 `.env.example`。

- [ ] Provider 可选 rules、mock、llm；真实模型地址、模型名和密钥通过环境变量配置。
- [ ] 实现 `handle_event`、`resume_loop`、`replan`；返回候选状态、证据引用和停止原因。
- [ ] 执行 OBSERVE → PLAN → ACT → VERIFY，必要时重新规划；ACT 必须实际调用白名单工具并读取结果。
- [ ] 提供两个同类事件分支：找到明确确认记录时停止缺口调查；未找到时补查其他允许来源，信息冲突则转人工。
- [ ] 每一步保存工具名、参数摘要、结果引用、计划版本、耗时；记录简短决策依据，不要求输出模型隐藏思维。
- [ ] 默认 8 次工具调用、30 秒运行预算，可配置；超时和非法输出保持未解决，不伪装正常完成。
- [ ] 模型输出中引用不存在的 Evidence、越权工具或任意状态字段时拒绝候选写入。

验收：真实工具结果改变下一步调用路径；同一 Intent 完成两次事件恢复和至少一次持久化重规划；mock 分支测试与真实模型验证分开报告。仅模型抽取一句话、固定执行工具列表或生成五个步骤名称不算本任务完成。

### T11 报告匹配 Verifier 与 Guard

涉及文件：新增 `backend/domain/{matching,verifier,guard}.py`、`backend/services/evidence_service.py`、`tests/domain/test_result_verifier.py`。

- [ ] 按患者、住院、项目、标本/医嘱、时间及版本验证匹配。
- [ ] 验证来源记录确实支持所引用主张，保存 supporting_evidence 与 searched_sources。
- [ ] 设定提醒宽限时间/截止点；报告刚到达不立即断言医生遗漏。
- [ ] 检查本项目已接入的医生确认操作和病程；无法覆盖医院全部来源时如实标记覆盖边界。
- [ ] 无确认候选提醒、已有明确确认、信息冲突、来源不全和查询失败分别返回。
- [ ] Guard 在事务内复核当前状态与版本，只允许可验证结果事件推进 RESULT_AVAILABLE。
- [ ] 迟到确认、报告修订会重新评估原 Finding，保留旧版本和审核历史。

验收：错患者、错住院和多报告歧义均不误关联；已确认不再作为当前缺口；一条有效报告本身不足以证明已查看或已处理。

### T12 审核 结果确认 关闭与审计

涉及文件：新增 `backend/routes/{findings,reviews,loops,audit}.py`、`backend/services/{review,audit}_service.py`、`tests/api/test_clinician_actions.py`。

- [ ] Finding 接受/驳回使用独立审核记录，驳回理由非空。
- [ ] `acknowledge` 明确绑定报告版本；`resolve` 要求医生处理结论；`cancel` 要求原因。
- [ ] “已知晓，等待药敏”可由模型形成提案，必须由医生确认对应查看、依赖任务及关闭选择。
- [ ] 追加 actor、时间、旧/新状态、理由、run_id 和幂等键，不覆盖原始 Agent 输出。
- [ ] 同时点击或旧页面提交返回冲突并要求刷新，重复请求不双写。
- [ ] 证据与审计查询也验证患者/住院访问范围。

验收：接受提醒不等于已查看，已查看不等于已闭环；医生可纠正误判；操作和父子任务关系可复盘。

### T13 医生端五个工作流视图

涉及文件：新增 `frontend/{timeline,loops,findings,evidence,trace,handoff}.js`、`frontend/workflow.css`、`tests/frontend/workflow.spec.js`；修改 `index.html`。

- [ ] Patient Timeline 展示事件时间、来源、操作者，点击回到原文。
- [ ] Open Loops 展示等待内容、负责人、下一事件、依赖、状态和到期标记。
- [ ] Workflow Gaps 展示待审核主张、报告版本和搜索范围。
- [ ] Evidence Drawer 展示来源记录，区分结构化来源核验与临床正确性判断。
- [ ] Agent Trace 展示实际调用、关键结果引用、预算和停止原因；mock/rules/llm 标识清晰。
- [ ] Handoff Draft 支持条目跳转和审核，具体数据由 T15 提供。
- [ ] 全部列表、按钮和详情接真实 API；取消旧请求，避免患者快速切换造成串数据。
- [ ] 提供加载、空态、失败、重试和键盘关闭弹窗；错误时保留未保存输入。

验收：不借助 Swagger 就能走通任务确认、报告查看、提醒审核与下一步操作；页面刷新后保持正确状态。

### T14 查房与病历草稿关联

涉及文件：修改 `backend/routes/records.py`、病历模型与迁移；新增 `backend/services/note_service.py`、`frontend/rounding.js`、`tests/api/test_rounding_records.py`。

- [x] 基于当前患者和查房原文生成草稿，替换固定病程示例。
- [x] 第一版允许模板整理或人工输入，模型润色另作可选能力。
- [ ] 保存原始文本、草稿、医生编辑版本与引用，不把未确认候选写成已执行事实。
- [x] 提供病历创建接口、正文和来源查看、保存修改与独立 signed_at。
- [ ] 已签署内容普通接口不可覆盖；病历签署不改 Loop 状态。

验收：不同患者不同输入生成对应内容；缺失信息不自动补为正常；数据库真实保存，可从病历查看来源查房和任务。

### T15 交班草稿与确认快照

涉及文件：新增 `backend/services/handoff_service.py`、`backend/routes/handoff.py`、`tests/api/test_handoff_snapshots.py`；完善交班视图。

- [ ] 根据指定住院的已确认事实、待审核 Finding 和全部未闭环 Loop 生成 SBAR 草稿。
- [ ] 高优先级未闭环事项必须展示，其余未闭环事项可完整展开；缺陷和不确定项明确标记。
- [ ] 保存 task/loop/evidence 引用、模板版本、生成时刻与数据版本。
- [ ] 医生可编辑正文/备注，不能通过编辑正文改变引用归属、任务状态或快照状态。
- [ ] 未处理高风险审核项阻止最终确认；仅等待外部结果的任务可以作为待跟进事项交班。
- [ ] 确认后不可覆盖；新事件到达标记旧草稿过期并允许生成新版本。

验收：药敏未确认任务能从交班点回证据；确认交班不关闭任务；旧快照可复现当时内容。

### T16 合成病例 回放与基线评测

涉及文件：新增 `eval/{corpus,replay,metrics,report}.py`、`eval/baselines/{rules,direct_llm,rag,workflow_agent}.py`、`tests/eval/`、`docs/eval/protocol.md`。

- [ ] 先建立 12 类回归场景，见第 8 节；冻结规则版输入与真值。
- [ ] 独立集至少 50 例（25 应提醒、25 不应提醒），开发样本与报告集分开。
- [ ] 重放通过生产 API/队列/Worker，除可控时间外不绕过业务链路；Agent 不得访问期望标签。
- [ ] 同一资料范围下比较 Rule Engine 与 Workflow Agent；Direct LLM/RAG 按需要增加，未运行不填结果。
- [ ] 分别统计意图提取、报告匹配和最终 Finding，给出 TP/FP/FN/TN、失败与弃权计数。
- [ ] 统计证据有效率、意图保留率、关闭判断、交班遗漏、工具次数与延迟。
- [ ] 数据集版本、seed、内容哈希、代码版本、provider/model/prompt 版本和运行环境写入报告。
- [ ] 分母为零标记 N/A；失败不从总样本中删除；人工节时指标待真实参与者实验后再报告。

验收：相同输入和规则结果可重复；真实模型报告可追踪配置但不假定逐字稳定；mock、离线回放和完整在线结果分表展示。

### T17 稳定性 预算和跨层约束

涉及文件：新增 `backend/worker/policies.py`、`tests/security/`、`tests/integration/test_failure_recovery.py`。

- [ ] 验证队列领取中断、业务提交后未完成 job、重试耗尽三种故障点。
- [ ] 对医生与 Worker 同时更新任务设置可验证的版本控制，冲突不丢医生操作。
- [ ] 预算、超时、工具错误和模型错误有不同停止原因；不关闭任务、不制造无记录结论。
- [ ] 测试外部记录内的诱导指令不能改变工具白名单或调用写 API。
- [ ] 只读工具、证据查询、报告修订和已确认交班均有权限与不可覆盖测试。
- [ ] 日志不输出密钥与完整临床原文，原始合成证据通过受控接口查看。

验收：失败可恢复、可解释，无跨患者状态变化；新增 Agent 能力不削弱原有审核边界。

### T18 扩展六类事件与四类缺口

依赖：M2 达标。本任务属于 M3，未完成时不得宣传完整覆盖。

涉及文件：扩展 contracts、工具、Verifier、fixtures、评测和 UI；新增 `docs/eval/coverage-matrix.md`。

- [ ] 新增 ORDER_UPDATED、CONSULT_NOTE_CREATED，连同已有四类达到六类事件。
- [ ] 逐类实现 PLAN_WITHOUT_ORDER、ORDER_WITHOUT_EXECUTION、EVIDENCE_MISSING_FROM_HANDOFF，连同结果未确认达到四类缺口。
- [ ] 增加 get_orders、get_consults 和执行状态来源，使用模拟数据只读，不开医嘱。
- [ ] 为每类定义适用任务、来源覆盖、时间窗、正常例、异常例和反例。
- [ ] 至少 100 条冻结轨迹回放，另安排每类完整在线生产链路验证。
- [ ] 能力矩阵分别标记“契约已定义、离线已测、在线已测、模型已测”。

验收：四类均有证据跳转和真实检测链路；不能用输出固定预期 Finding 的生成器代替检测器。

### T19 MCP 与 AgentArts 平台适配

依赖：M2 达标。按比赛要求实施，不影响本地版本可运行。

涉及文件：新增 `backend/mcp/server.py`、`backend/providers/agentarts.py`、`tests/platform/`、`docs/platform/agentarts-runbook.md`。

- [ ] 在现有只读工具外包装真实 MCP SDK 服务，定义明确 transport 和输入输出 schema。
- [ ] 用 MCP client 做实际握手、工具列表和调用测试；本地 Registry 不作为协议验收结果。
- [ ] 统一平台与本地 Run/Loop/Intent ID，跨调用恢复以本地持久化状态为准。
- [ ] 完成平台适配器并在具备可用权限时做发布运行时全链路测试。
- [ ] 平台密钥、区域、模型与费用配置外置；付费部署、启用收费资源需单独获得授权。
- [ ] 区分画布试运行、发布运行时和本地 Worker 的报告；没有实测则标为未验证。

验收：平台返回能经过同一 Guard、证据和审核流程；平台故障可退出且本地任务不丢。只有实际协议/运行时报告才声明已集成。

### T20 联调 演示 CI 与版本交付

涉及文件：新增 `scripts/{run_demo,verify_release}.py`、`docs/demo/{runbook,script}.md`、`docs/release/checklist.md`、`.github/workflows/ci.yml`、`CHANGELOG.md`；更新 `README.md`。

- [ ] 在专用数据库从迁移、种子、API、Worker 到前端重复启动，不运行清空用户库/卷的命令。
- [ ] 提供主剧情的一键合成事件演示，打印 event、intent、loop、run 和 finding ID。
- [ ] CI 覆盖后端规则/接口/迁移、前端核心浏览器流程、静态检查和密钥检查。
- [ ] 明确发布层次 M1/M2/M3、实际完成项、未测项、运行方式和报告路径。
- [ ] 验证现有病历功能、全部新视图和 API 错误场景；已实现 API 与规划 API 分开列。
- [ ] 检查暂存区不包含 `.env`、缓存、数据库和录音；保留 `.env.example` 占位模板。
- [ ] 提交/上传沿用本项目明确授权与仓库设置；不机械采用对方团队分支/评审规则，不重写历史或强推。

验收：新环境按文档可运行；演示输出与 UI、数据库一致；当前发布的能力和测评结果可验证。

## 7 关键接口草案

保留已有 `/api/medical-records` 路径，新增接口在 T03 冻结。所有资源读取与写入检查授权范围；写操作带幂等键与期望版本。

| 接口 | 用途 |
| --- | --- |
| GET `/api/patients` | 当前医生的患者列表 |
| GET `/api/patients/{id}/encounters` | 可访问住院列表 |
| POST `/api/rounding-sessions` | 保存查房并提交事件 |
| POST `/api/events` | 开发模式导入模拟事件 |
| GET `/api/encounters/{id}/timeline` | 事件时间线 |
| GET `/api/encounters/{id}/intents` | 目标与候选意图 |
| POST `/api/intents/{id}/confirm` | 确认候选并建立 Loop |
| GET `/api/encounters/{id}/loops` | 任务看板 |
| GET `/api/loops/{id}` | 当前状态与依赖 |
| POST `/api/findings/{id}/reviews` | 接受或驳回提醒 |
| POST `/api/loops/{id}/acknowledge` | 确认指定报告版本 |
| POST `/api/loops/{id}/resolve` | 保存医生结论并关闭 |
| POST `/api/loops/{id}/follow-up` | 提出并确认后续等待环节 |
| GET `/api/evidence/{id}` | 查看原始证据与版本 |
| GET `/api/loops/{id}/runs` | 查看调查与恢复链 |
| POST `/api/encounters/{id}/handoffs` | 生成交班草稿 |
| POST `/api/handoffs/{id}/confirm` | 医生确认快照 |
| POST `/api/medical-records` | 保存病历草稿及来源 |

## 8 必须通过的回归场景

| 编号 | 场景 | 预期 |
| --- | --- | --- |
| R01 | 新查房提出随访 | 产生候选，医生确认后等待 |
| R02 | 报告尚未返回 | 保持等待，无结果未确认告警 |
| R03 | 正确报告返回，满足提醒条件且未找到确认 | 生成证据完整的待审核 Finding |
| R04 | 已有明确确认记录 | 不制造当前未确认告警 |
| R05 | 接受/驳回提醒 | 保留审核，不隐式确认/关闭 |
| R06 | 医生确认并继续等药敏，药敏返回 | 同一 Intent、关联 Loop、两次恢复、一次计划变化 |
| R07 | 换班 | 所有高优先级未闭环任务进入交班，可回到证据 |
| R08 | 错患者/错住院/同项目多标本 | 不误匹配，歧义转待核实 |
| R09 | 重复事件、重复点击、Worker 中断 | 业务幂等，状态恢复，审核不丢 |
| R10 | 报告先到、报告修订、迟到确认 | 回查有效版本，保留历史并重新评估 |
| R11 | 否定句、历史描述、来源不全、工具/模型超时 | 不擅自建确定任务，不把失败当阴性 |
| R12 | 越权访问、正文注入、旧版提交、快照修改 | 服务端拒绝，无串数据或历史覆盖 |

## 9 发布检查与评测目标

M1：R01–R12 中适用于规则闭环的测试全部通过；数据持久、审核分离、证据有效、交班不漏关键任务。没有真实模型调用时发布名和文档明确写规则版。

M2：额外证明一个主 Workflow Agent 实际选择和执行只读工具；同类输入因工具结果不同产生不同路径；同一意图经历两次暂停/恢复和一次有前后版本的重规划。不能以 mock 的正确输出代替真实模型效果。

M3：逐类证明六事件/四缺口及平台能力，不以存在 schema、样本或适配器作为完成依据。

建议算法目标为独立合成集精确率 ≥90%、召回率 ≥80%；这是拟定目标，不是现有成绩，也不是临床标准。工程硬条件：跨患者/住院错配为 0、未经医生操作的高风险关闭为 0、已发出证据引用有效率 100%、定义范围内高优先级交班遗漏为 0。报告所有计数、样本量和失败，不用平均成绩掩盖隔离错误。

## 10 排期和第一批任务

建议按单人有效开发日预留：

| 阶段 | 工作内容 | 估计 |
| --- | --- | --- |
| 基础 | T01–T05 | 6–9 日 |
| 规则与后台 | T06–T09、T11–T12 | 8–11 日 |
| 医生端与业务衔接 | T13–T15 | 5–7 日 |
| 验证交付 | T16–T17、T20 规则部分 | 4–6 日 |
| 真实 Agent | T10 与相应评测、交付 | 5–8 日 |
| 比赛扩展 | T18–T19 | 视范围及平台条件另估 |

M1 约 23–33 个有效开发日；M2 累计约 28–41 日。多人并行可缩短，但不把对方文档中的四周直接当作本项目承诺。

第一批只执行 T01 → T02 → T03 → T04 → T05，目标是“当前病历可信保存、住院明确隔离、查房能产生持久化事件”。然后推进规则闭环，再按 T10 的明确标准实现真实 Agent。T13 的界面骨架可在 T03 后开发，所有业务状态必须以后端为准。

## 11 本版与旧计划的关系

保留旧 `docs/plans/MVP_TASKS.md` 作为早期规则方案参考；它没有覆盖完整动态 Agent 验收。本版用独立 Intent 与 OpenLoop 取代简单单表任务，以 T10 明确模型/工具/重规划交付，并将四类缺口和平台能力列为可选 M3。后续勾选、实现和发布以本文件的任务编号与所选交付层次为准。
