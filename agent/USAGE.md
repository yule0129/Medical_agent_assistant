# ClinLoop Agent A01–A07 实现说明

当前实现已覆盖从运行契约到前端审核交接的代码链路。A02～A07 使用明确标记的注入测试模型验证；用户已在本地配置密钥，三类真实 DeepSeek 合成调查通过，实际轨迹保存在 docs/verification/deepseek-smoke-2026-10-25.json；完整浏览器长程流程和故障场景仍明确使用注入测试模型。A08 独立合成回放评测未实施。

## 各模块与文件

| 模块 | 实现 | 验证边界 |
| --- | --- | --- |
| A01 运行契约 | contracts.py：输入、身份、四类动作、候选、预算和停止原因 | 27 项契约测试 |
| A02 只读工具 | tools/reader.py、tools/evidence.py：报告、病程、任务、确认记录、当前证据、住院摘要 | 查询数据库；服务器注入范围，时间过滤、截断、完整性、内容摘要校验 |
| A03 模型适配 | llm/deepseek.py、llm/prompts.py：DeepSeek JSON Output，严格解析动作，硬超时，无隐藏回退 | HTTP MockTransport 验证请求/错误/超时；三类真实调用通过 |
| A04 动态调查循环 | core/loop.py、runtime/worker.py：逐轮模型决定工具，回传真实结果，计数、超时、来源和轨迹 | 本地分支测试与三类真实 DeepSeek 分支通过 |
| A05 证据与审核 | guard/evidence.py、api/review.py：引用、摘要、版本、范围、实际覆盖与宽限期校验 | 拒绝伪造、旧版本、变化原文；医生显式决定业务操作 |
| A06 恢复与重规划 | AgentJob、事件队列、租约、前序 Run、依赖任务、到期事件 | 注入模型两次事件恢复、计划变化、独立进程恢复 |
| A07 医生工作台 | agent/frontend/workspace.js：模型状态、轨迹、引用、审核、病程、交接快照 | 隔离浏览器完整流程与 PostgreSQL 回滚验证 |

schema 仅校验结构。运行器实际执行工具，Guard 对照实际查询结果核验来源及覆盖；模型不能通过声明 query_succeeded 或伪造 ID 获得权限。确认响应仍不是任务处理完成。临床正文和报告中的指令仅作为数据处理。

## 本地配置与启动

在根目录本地 .env 填写下列项，密钥不进入仓库。默认模型来自当前 DeepSeek 文档示例，可按账号支持情况修改。

```dotenv
DEEPSEEK_API_KEY=在本地填写真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
AGENT_ENABLED=true
AGENT_MAX_TOOL_CALLS=8
AGENT_MAX_MODEL_REQUESTS=8
AGENT_TIMEOUT_SECONDS=30
```

API 与 Worker 都会在启动时加载 .env，修改后两个进程都要重启。

```powershell
python -m alembic upgrade head
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
# 另开终端
python -m backend.worker.main --poll-interval 1
# 另开终端
python -m http.server 5500 --bind 127.0.0.1
```

0005 只新增 agent_jobs 和 agent_proposals，不清空已有病历。Agent 调查队列独立于临床事件处理事务：先保存临床事件和规则核验结果，再由短事务领取并持久化模型调查租约，网络期间不持有临床写锁；提交候选时重新检查版本。默认单次 8 次工具、8 次模型请求、30 秒。失败显式显示并允许医生重试；崩溃后的过期租约最多自动恢复 3 次，业务副作用仍需审核。

## 前端操作

1. 查房保存血培养随访任务，医生确认时填写明确标本号，如 BC-001。
2. 打开任务详情中的“医疗 Agent 调查”。无密钥时明确显示待配置，启动按钮禁用。
3. 配置后点击“启用 Agent 并调查”，查看队列；后台 Worker 运行后可刷新读取实际模型、工具、参数、耗时、停止原因和来源。
4. 模拟报告返回时先完成确定性身份/版本核验，再自动排入该目标的 Agent 调查。无需让模型负责确认报告的身份事实。
5. 在面板保存“已知晓本次血培养报告，继续等待同一标本药敏”等病程草稿，Worker 接收事件后调查。草稿不会自动变成已确认响应。
6. 候选引用可展开查看当时原文，并读取当前来源核对。可以仅接受、驳回、明确确认已查看，或明确核对报告并批准后续任务。批准后续任务不自动完成原任务；处理结论仍单独记录。
7. 新报告版本、被编辑的原文、过期任务版本会阻止旧候选提交，需重新调查。后台更新仅提示刷新，不覆盖未提交输入。
8. 交班摘要包含当前未完成任务、证据、确认情况、负责人及前序任务。编辑保存为新版本；确认时复核任务集合与版本，已确认内容保留，交接不关闭任务。

Agent 只跟踪医生明确启用的目标及审核生成的依赖目标。到期按 due_at 产生内部复核事件，同一到期时间和任务版本不重复提醒。对未完整查询或尚在 5 分钟响应宽限期内的报告，不形成“未发现响应”的候选。

## 主要接口

所有接口前缀 /api/agent；仍使用开发 doctor_id 范围校验，无生产登录授权。

| 方法与路径 | 用途 |
| --- | --- |
| GET /status | 配置状态，不返回密钥 |
| POST /loops/{id}/runs | 显式启用并排队；需要版本与 request_key |
| GET /loops/{id} | 调查队列、实际步骤、候选、记忆 |
| POST /jobs/{id}/retry | 医生明确重试失败调查 |
| POST /loops/{id}/progress | 保存医生病程草稿及事件，幂等 |
| POST /proposals/{id}/review | 审核候选；独立选择仅审核/确认响应/批准依赖任务 |
| GET /sources/{kind}/{id} | 在授权范围读取当前原始来源 |
| GET /encounters/{id}/handoffs | 历史交接快照 |
| POST /handoffs/{id}/revisions | 编辑为新版本，保留原内容 |
| POST /handoffs/{id}/confirm | 核对任务/证据版本与集合后确认 |

## 验证与在线验收

```powershell
python -m pytest -q
python tests/postgres_rounding_smoke.py
python tests/browser_rounding_smoke.py
# 填好本地密钥后，显式执行真实 DeepSeek 验收
python -m agent.evaluation.smoke --scenario all
```

在线脚本只创建临时合成数据库，不读取开发库病历。分别检查 missing、found、follow 三类候选及实际工具路径；任一未达预期返回非零退出码，不伪装通过。无密钥时明确退出；这是少量在线验收样例，不替代 A08 的独立评测。

接口依据：[DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。使用 JSON Object 输出并在本地进行 schema 校验，不声称供应商保证整个业务 schema。


## 本轮真实在线结果 2026-10-25

使用 deepseek-flash，显式关闭默认 thinking，采用 JSON Object 输出并由服务端校验。三个隔离合成场景全部得到通过 Guard 的候选：

| 场景 | 实际工具路径 | 候选 |
| --- | --- | --- |
| 无明确响应 | get_lab_reports → get_result_acknowledgements → get_progress_notes | RESPONSE_NOT_FOUND |
| 已有确认 | get_loop_state → get_lab_reports → get_result_acknowledgements | RESPONSE_FOUND |
| 病程提出等待药敏 | get_lab_reports → get_result_acknowledgements → get_progress_notes | FOLLOW_UP |

原始合成轨迹：[在线验收记录](../docs/verification/deepseek-smoke-2026-10-25.json)。后两类各出现一次格式修复请求，已计入模型请求预算并留痕。三条样例不代表稳定准确率或临床有效性。确认后的跨事件长程链路、旧版本拒绝及故障恢复由本地注入模型测试和浏览器验证覆盖。

API 依据还包括 [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。JSON 格式修复是可见、有预算的再次请求；不会伪造模型输出或静默转用规则。


## 医生反馈入口 2026-10-25

首页新增“Agent 工作反馈”，每 5 秒自动读取 GET /api/agent/feedback。按医生授权住院汇总待审核、需人工协助、失败、排队、调查中、等待、已审核和已完成状态。反馈来自数据库中的调查和审核记录，刷新不会调用模型或提交审核。点击直接进入对应任务证据与审核。

任务列表显示每项 Agent 进展，系统通知进入反馈中心；任务详情自动更新调查输出，同时保留未提交病程、处理结论、审核备注和焦点。候选已变化导致输入框消失时，原备注暂存在当前页面供复制。新增需处理的结果只在状态变化时提示，刷新页面仍可读取待办。

报告修订、原文修改或后续病程更新使旧候选进入需重新调查状态，不计为可直接审核候选。接受调查发现不确认结果、不完成任务；明确响应与后续任务直接提供对应的医生确认/批准按钮。审核后首页待处理计数同步更新。

新增 5 项反馈契约检查，完整 pytest 共 93 项通过；隔离浏览器验证自动显示新候选、输入保留、反馈中心跳转、审核后计数消退。前端反馈检查使用注入 test-model，未重复调用真实模型。

旧任务若已登记手工结果但缺少报告匹配编号，启动按钮禁用并说明原因；可建立带标本号/医嘱号的后续随访再启用 Agent。尚无结果的等待任务则先补充匹配条件。不会修改旧手工记录来假装已自动匹配报告。

## 入院记录Agent（2026-10-26）

运行迁移到0007后，患者详情中的入院记录支持可编辑问诊转录、检查汇总、Agent生成草稿及医生审查签署。模型使用同一份DeepSeek配置，API进程须能访问模型服务；仅配置密钥并不验证网络。问诊和检查修改后，旧草稿需重新生成再审查。语音依赖浏览器，暂未提供独立ASR和自动说话人分离。在线合成验证使用 `python -m agent.admission.smoke`，不写开发数据库；验证轨迹见 docs/verification/admission-deepseek-2026-10-26.json。当前110项自动测试通过，A08独立效果评测仍待开展。
