# Agent 素材执行衔接修复方案

日期：2026-10-21。状态：第一批已实现并完成定向回归，第二批待实施。核对基线：`eeaec8f1`。本文不授权生成、审批、重启服务或修复现场数据。

## 1. 目标与取舍

让 Agent 从返回结果直接知道：哪些内容已经保存、下一步由谁处理、调用什么工具、使用哪个准确版本。未提交的草稿不能显示为等待用户确认，协调器正在处理不能显示为素材正在生成。

采用现有函数的局部修复。只修改提示词无法消除相互矛盾的状态；新建通用状态机、后台补偿器或统一执行平台超出本次需要。

明确撤回前面讨论中的“ready 草稿遗漏提交后自动唤醒”建议。本方案不新增自动重试、自动补交、自动唤醒、轮询器、退避计时器或重试次数配置。

### 执行边界

- 正常顺序执行：成功准备草稿后，Agent 在当前已授权回合调用既有确认工具一次；这是下一步操作，不是失败重试。
- 操作失败或回合结束仍未提交：保存准确断点并停止，使用现有显式继续入口；不另起 Agent 回合补做。
- 工具结果中的 `next_action` 只是建议，不触发执行，也不是授权凭据。
- 保留既有真实任务完成、审批答复与画布回执的事件处理；不扩大事件可触发的行为。
- 保留现有素材失败后的 `manual_resume_required` 闸门，观察器不得清除。
- 幂等是显式重复请求的保护，不是系统主动重发请求的理由。
- 不自动切换模型、渠道、素材策略，不自动创建替代工作包或草稿。

## 2. 证据与适用范围

以下现场数据来自此前只读排查。实施时需要重新读取当前状态，不能将这些历史引用当作执行输入。

| 已核对事实 | 对应实现证据 | 本次修改 |
| --- | --- | --- |
| 17:36 的素材草稿 ready，返回准确确认工具；后续没有确认工具调用 | `.hermes/plugins/freezone/director_tools.py` 已返回 `next_tool`；续跑收尾没有检查该提交是否完成 | 返回同一准确动作；收尾保留断点并停止 |
| 17:37 进度同时返回 running、work_ref=null、next_action=null、用户工作包选择阻塞、user_action_count=0 | `production_inventory._execution` 将 resuming 映射为 running；状态投影及动作选择因此抑制下一步 | 运行状态按原生执行证据判断 |
| ready 草稿没有审批回执仍显示 awaiting_confirmation | `material_work.material_lifecycle_status` 的现有分支及测试 | ready 改为待提交；审批必须有真实请求证据 |
| 工作包已保存，但委托未关联 | `/director/material-work` 未选择工作包；进度只认明确关联工作包 | 在明确创建命令中提供关联意图 |
| 后端要求用户选择工作包，前端测试要求不出现第二个工作包选择 | `production_state.build_production_state` 与 `ReferenceProductionPanel.test.tsx` | 正常创建直接明确关联；历史选择通过普通对话和已有选择工具完成 |
| 缺准入时返回失败，但工作包已保存 | 材料工具返回 work_ref 和 saved 生命周期；`_material_work_failure` 默认 side_effects=none | 归一化保留实际已保存引用和状态 |
| 审核工具允许 plan/purpose，后端只允许 plan/goal | `director_tools` 的证据 schema 与 `production_review.review_production_plan` | 后续独立小改修 schema 和错误定位 |

最小输入已复现“仅 resuming 导致 running”和“ready 无回执显示 awaiting_confirmation”。当前确认工具已注册；没有证据证明这次是注册故障。审核累计 16 次调用，其中 8 次调用被拒绝，不能把其余调用都称为审核通过。

### 不据此推断的事项

- 不根据工具总数断言工具太多，不重构工具发现系统。
- 不根据 10 秒视频断言素材方案错误；可能是局部制作。已确认的是工作包未填写 placement/take/turn 对应关系。
- 不承诺性能或成功率提升百分比；本次没有可用于该结论的对照数据。

## 3. 第一批：修复已证实的执行断点

### 3.1 在现有素材代码中读取执行证据

修改 `material_execution.py`，增加一个只读辅助函数供生命周期、进度与续跑检查复用。正常读取只处理明确选中工作包各步骤已经保存的 draft_id、draft_revision、run_id 和实际回执，不扫描并采用最新或唯一工作包。回合收尾允许核对本回合工具真实返回的工作包/草稿，但该核对不能改变委托选择。

不新建数据库表、持久化状态枚举体系或后台服务。辅助函数的返回值是即时观察结果，不另存一份权威状态。已有数据缺失时报告缺证据，不根据名称或创建时间补齐。

查询草稿时核对 project、canvas、准确版本及与工作包的绑定。待审批请求还必须匹配这次确认尝试并且仍有效，旧卡片不能证明当前等待用户。已有审批/桥接存储只读复用。

| 当前证据 | 生命周期与进度 | 下一步 |
| --- | --- | --- |
| 工作包已保存、没有草稿 | 保留已有 generation_readiness；没有原生任务 | 准备现有工作包，或补齐真实缺失参数 |
| 草稿 ready 且没有提交 claim | canvas_delivery_status=ready_to_submit；generation_status=not_started；execution_status=waiting_agent | submit_canvas_approval |
| 草稿 confirming/submitted，存在当前有效审批请求 | canvas_delivery_status=awaiting_confirmation；execution_status=waiting_user | 展示真实请求 |
| 草稿 confirming/submitted，没有有效审批请求，也没有交付回执 | canvas_delivery_status=pending；不宣称等待用户或素材生成中 | 有真实有效交付尝试才等已有事件；任务失败或无法核实交付对象时保留引用并报告交付未核实 |
| 当前图交付回执 delivered，无原生生成任务 | canvas_delivery_status=delivered；generation_status=not_started | 按既有执行模式判断下一步，不擅自启动任务 |
| 与当前工作包绑定的原生任务 queued/running | execution_status=running；保留准确任务引用 | 等已有任务事件 |
| 候选/验收/原生失败 | 沿用现有候选、验收和失败处理 | 对应审看或明确恢复动作 |

优先保留明确停止、取消和真实失败；已确认的图交付不能被晚到的提交结果降级。`run_after_create=false` 时交付不等于需要生成。

步骤当前在绑定 ready 草稿时就被记为 submitted。第一批不迁移历史步骤状态；对外生成进度根据真实草稿、任务、候选判断，不再单凭该字段推断已开始生成。

### 3.2 进度不再把协调器活跃当成素材运行

修改 `production_inventory._execution`，在素材阶段不再仅凭 submitting/analyzing/analysis_ready/resuming 推导业务任务 running。只有当前范围的原生执行证据可以支持任务运行状态。原片分析及其他阶段的既有真实任务判断保持独立，不全局删除协调器状态分支。

在 `production_state.py` 和 `production_advancement.py` 保留已有停止/失败规则；修正正常素材状态下的下一步推导。协调器正在处理时也应能读到尚未完成的 Agent 动作。没有素材工作包时，区分“需要创建”和“已有候选需明确选择”，不能统一要求用户找确认卡。

有效用户动作计数从同一批真实用户动作计算。工作包选择可以通过普通对话处理，画布审批必须有真实请求；两者不能共用“等待已有确认卡”的说明。

### 3.3 准确下一步沿用现有字段

不再发明另一套动作协议。保留现有 `next_action`，其 Agent 动作使用项目已有 `tool`、`draft_id`、`revision` 字段；准备工具已有 `next_tool` 从同一动作生成。

拟议的 ready 草稿进度示例，示例 ID 仅用于说明：

```json
{
  "execution_status": "waiting_agent",
  "next_action": {
    "owner": "agent",
    "kind": "submit_canvas_approval",
    "action_id": "submit:draft-example:1",
    "tool": "freezone_confirm_workflow_draft",
    "draft_id": "draft-example",
    "revision": 1
  }
}
```

`action_id` 只是当前准确动作身份，不建立新任务、消费队列或自动执行记录。工具适配层依据这个结果生成提示；不再另外凭阶段自由推导确认卡是否存在。

当前有手动恢复闸门时，显示明确继续入口和保留的草稿引用，不用 ready_to_submit 覆盖该闸门。确认工具自身继续校验权限、版本、准入和执行模式。

### 3.4 创建命令明确关联工作包

在现有 `MaterialWorkRequest` 和 `freezone_prepare_material_work` 增加可选布尔参数 `select_for_commission`，默认 false，保持旧调用的候选保存行为。新版本素材工具说明：为当前已确认委托创建实际制作工作包时显式设为 true；单纯创建候选时保持 false。

该字段只表达“将这次创建的工作包设为当前工作”，不是内容通过或生成批准。宿主使用当前已认证请求、commission_ref、plan_ref、command_id 验证关联，不从聊天措辞猜测。

实现时复用工作包现有 command_id 与委托 decisions，不新增工件 schema 字段。关联成功记录包含创建 command_id、原始 commission_ref、select_for_commission 和准确 work_ref，区分创建关联与用户选择，不伪造用户消息凭据。

处理顺序：

1. true 时，先在既有画布写锁内检查当前准确委托、权限及方案；已有其他工作包则返回冲突，不保存一份新工作包再覆盖它。若同一 command_id 已有工作包但没有匹配的成功关联记录，则返回其 work_ref 和“使用现有选择入口”，不把历史候选请求改成创建并采用。
2. 使用现有稳定 command_id 保存或幂等找到同一工作包。
3. 复用现有选择校验与发布逻辑关联该准确工作包，记录创建命令来源。普通用户选择仍使用现有来源；不得伪造成用户确认消息。锁内逻辑按现有锁顺序拆成可复用函数，避免重复获取非重入锁。
4. 返回最新 commission_ref、work_ref 和 material_lifecycle。工具后续草稿绑定成功时使用绑定接口返回的最新 work_ref 和 work 计算结果，不能继续使用绑定前的 work。

工件保存和委托修订是两个持久化步骤，不声称跨文件原子事务。若中间中断：保留已保存 work_ref，显式恢复时读取它并调用已有选择入口；不新建一套关联意图、检查点或补偿机制。若委托或工作内容已经变化，则返回当前引用和冲突，不追随新版本重做或创建另一份。

成功请求重放时核对已保存的 select_for_commission、创建 command_id、原始 commission_ref 与 work_ref；请求语义变化返回冲突。既有候选创建后需要采用时，应走单独的现有选择命令，不能把相同创建命令当成一次新选择。

`reference_production_observer._saved_work` 改为只读取明确关联工作包，与进度口径一致。历史未关联工作包仍由现有候选列表建议，用户可在普通对话明确选择，Agent 再调用既有 freezone_select_material_work；有歧义必须先澄清。GET 不采用候选，前端不新增一张工作包确认卡。

### 3.5 未提交时停止并保留断点，不自动补做

修改 `reference_remix_continuation._resume_agent` 的收尾检查：只在当前素材阶段、当前回合确实产出/读取了匹配草稿的范围内判断遗漏提交；不要全局扫描历史 ready 草稿并激活它们。

- ready 且没有确认 claim/有效审批请求：记录 `material_draft_not_submitted`、当前工作包及草稿引用、next_action；复用现有 resume_failed 和 manual_resume_required 保存，说明“草稿已保存，尚未提交，等待明确继续”。
- 已有 claim、审批请求、交付或任务：按真实事实等待，不重新调用确认工具。
- 用户取消、明确停止、现有素材失败闸门：优先保留，不被 ready 草稿改写。

不改写整段 Agent 对话来伪造“已提交”。将确定性断点通过现有工具状态/恢复信息展示；若需要解释，只说明真实状态。不新增卡片类型或自动追加一轮 Agent 请求。

明确继续后，复用现有 retry/resume 入口，由 Agent 读取保存的准确引用：ready 才调用既有确认工具；confirming/submitted 查询已有请求；confirmed 检查已有回执。版本冲突、过期或工具不可用则报告对应原因并停止，不自动 prepare 一份替代草稿。

### 3.6 失败归一化保留已保存结果

修改 `_material_work_failure`，保留有界的 work_ref、draft_id/revision、operation_id、material_lifecycle 和现有恢复动作。缺省 side_effects 不能在已保存结果存在时写成 none；证据不全时用 unknown。保留传输层 ok/status 及失败，不将部分保存包装为全部成功。

第一批保留现有失败停止策略：不因为后续聊天文字、另一工具成功或 ready 草稿自动清除手动恢复闸门。不新增“修复成功自动放行”的操作映射或失败归并引擎。

## 4. 文件与实施顺序

下表路径相对于仓库根目录。每组先补失败回归，再做最小实现，再运行相应测试；不机械拆出大量模块。

| 顺序 | 修改位置 | 独立交付与测试 |
| --- | --- | --- |
| 1 | `src/novelvideo/freezone/remix/material_execution.py`、`material_work.py`、`production_inventory.py`、`production_state.py`、`production_advancement.py`；`.hermes/plugins/freezone/director_tools.py` | 真实生命周期及准确 next_action。测试：`tests/test_material_workflow_status.py`、`test_material_execution.py`、`test_reference_production_state.py`、`test_reference_production_state_api.py`、`test_freezone_plugin.py` |
| 2 | `src/novelvideo/api/routes/reference_director.py`；`src/novelvideo/freezone/remix/material_work.py`、`reference_commission.py`；`.hermes/plugins/freezone/director_tools.py`；`src/novelvideo/chat/reference_production_observer.py` | 创建时明确关联、幂等重放、观察器口径一致。测试：`tests/test_reference_director_api.py`、`test_reference_commission.py`、`test_reference_material_selection_api.py`、`test_reference_observer_scheduling.py` |
| 3 | `src/novelvideo/chat/reference_remix_continuation.py`；必要时仅在 `frontend/src/api/referenceDirector.ts` 补返回类型 | 准确失败引用及未提交断点，手动恢复闸门不变。测试：`tests/test_reference_remix_continuation.py`、`test_reference_observer_scheduling.py`、`frontend/src/features/superchat/ReferenceProductionPanel.test.tsx` |

第 2 组同步更新三份既有 Agent 工作流说明中的素材段落：`src/novelvideo/agent_skills/dramaclaw-workflows/references/reference-video-remix.md`、`agent-kit/skills/dramaclaw-workflows/references/reference-video-remix.md`、`.hermes/skills/workflows/references/reference-video-remix.md`。只补创建关联参数、准确提交下一步和失败停止边界，不加入新的自动执行规则。

若既有类型或输出字段白名单阻止新增事实，按真实返回补齐对应声明，不扩展成全项目接口重构。现有生命周期消费者需要搜索并同步更新；持久化 workflow_drafts 状态、确认 claim 和回执协议不改。

## 5. 验收用例

- [ ] 没有工作包和任务，仅 coordinator=resuming：不能返回素材 running，不能指示 Agent 等待不存在的任务。
- [ ] ready 草稿无审批请求：ready_to_submit + waiting_agent + 准确确认工具；用户待审批数为 0。
- [ ] 手动模式产生真实审批请求：显示 waiting_user 与真实请求，普通聊天中的“请确认”不算证据。
- [ ] 自动模式真实交付：直接显示回执状态，不要求找确认卡。
- [ ] 图已交付但 run_after_create=false 或任务尚未登记：不显示生成中。
- [ ] 真实原生任务 queued/running、失败、候选和已验收结果：保持各自准确状态。
- [ ] 新建并明确采用：委托可读取该工作包，返回最新引用，观察器与进度一致；选择不批准生成。
- [ ] 已选择其他工作包：新建采用请求冲突，原关联不变；跨用户、跨项目、跨画布、过期版本仍拒绝。
- [ ] 候选创建默认不采用；GET、唯一候选建议和历史扫描都不修改选择；普通对话明确选定后可调用现有选择工具。
- [ ] 创建保存后关联前中断：显式重放返回原 work_ref 和已有选择入口，不补造关联、不创建第二份；用户明确继续后可通过现有选择命令关联。
- [ ] Agent 在 ready 后直接结束：保存 material_draft_not_submitted 和准确引用，不发第二轮模型请求、不调用确认、不生成任务。
- [ ] 断点后旧观察事件、重复事件和后台扫描不能绕过 manual_resume_required。
- [ ] 明确继续 ready 草稿使用原 ID/版本；已提交或已确认草稿不重复创建节点/任务；晚回执不降级 confirmed。
- [ ] 缺准入但已有工作包：失败结果及 run 保留 work_ref/saved 生命周期，不声称 side_effects=none，不自动补准入。
- [ ] 字段错误、版本冲突、服务端错误、工具不可用：只返回真实原因及引用，不自动重发、重建或切换模型。

### 验证命令

实施后先运行改动涉及的定向用例，再运行以下回归；任何失败先归因，不用反复重跑代替修复。

```bash
uv run pytest tests/test_material_workflow_status.py tests/test_material_execution.py tests/test_reference_production_state.py tests/test_reference_production_state_api.py -q
uv run pytest tests/test_reference_director_api.py tests/test_reference_commission.py tests/test_reference_material_selection_api.py -q
uv run pytest tests/test_reference_remix_continuation.py tests/test_reference_observer_scheduling.py tests/test_freezone_plugin.py -q
```

在 `frontend/` 下运行：

```bash
pnpm exec vitest run src/features/superchat/ReferenceProductionPanel.test.tsx
```

最后运行 `git diff --check`，检查没有新增调度/重试调用、数据库表或审批绕过。上述为可复用的验收命令，本次实际验证范围和限制见第 9 节。

## 6. 第二批：独立的输入与错误改进

不阻塞第一批，但不能靠重试掩盖，分别做小改：

1. 在 `director_tools.py` 将 plan 证据字段约束为 goal，`production_review.py` 返回 finding/evidence_ref 的准确错误路径及实体；不改成替 Agent 自动选证据或自动通过审核。测试 `tests/test_production_review.py`、`test_freezone_plugin.py`。
2. 视频生成输入在工具边界明确 durationSec 的整数范围；后端仍检查当前模型支持范围。无依据不自动填默认时长，不新增付费预检请求。
3. 复用当前 plan_diagnostics/工作包目标绑定信息报告素材覆盖缺口。允许有明确目标的局部制作，不以“时长不等于成片”作为全局拒绝条件；不在本次增加完整覆盖规划器。

## 7. 兼容、现场与发布

- 新创建参数默认 false；持久化工作包、步骤、草稿 schema 不做迁移。关联结果沿用现有委托 decisions；该过程会修订委托版本，所有返回必须携带最新 commission_ref，不能继续复用请求版本。
- ready_to_submit 为对外投影新增值，后端、工具输出与实际消费者同批更新。旧 submitted 步骤按草稿/任务证据解释。
- 历史未关联工作不自动迁移。当前现场恢复必须先核对方案目标和素材绑定，再通过已有显式选择/继续入口处理，不能直接执行本文记录的旧草稿。
- 已确认、已提交和运行中的任务仍通过原 claim/回执/幂等机制保护，不批量重置状态。
- 本计划不包含服务重启或在线生成验证。后续如需更新常驻工具进程，应先确认活动任务并使用既有运维方式；不能用重启代替状态修复。

## 8. 与已有文档的关系

本地历史设计 `docs/superpowers/specs/2026-10-20-material-state-separation-design.md` 中 ready ⇒ awaiting_confirmation 的约定由本方案细化。`docs/superpowers/` 当前被 Git 忽略；该历史文件仅供背景，不作为实施必须存在的依赖，也不为同步它修改忽略规则。

沿用 `docs/superpowers/plans/2026-10-18-material-work-error-and-stop-auto-resume.md` 的失败停止边界，以及 `docs/experiments/agent-execution-boundaries.md` 的不自动重试约束。本次不通过“自动补交”“自动续办”等改名方式绕过这些边界。

## 9. 第一批实施记录

第一批已实现第 3 节的证据读取、状态投影、创建关联、未提交断点与失败引用保留；没有新增自动重试、补交、唤醒或调度调用，没有数据库迁移。第二批不在本次改动范围。

- 后端定向回归：925 passed，2 deselected，12 个依赖及 FastAPI 既有弃用警告。覆盖第 5 节列出的测试文件，并包括 `tests/test_material_generation_controls.py`。
- 前端 `ReferenceProductionPanel.test.tsx`：11 passed。
- 对本批改动运行 pre-commit：gitleaks、禁用词检查通过；`git diff --check` 通过。
- 排除 `test_canvas_read_mode_is_host_selected_and_default_clears_inherited_mode`：隔离测试环境未配置其依赖的模型网关密钥。
- 排除 `test_observer_wakes_saved_review_when_run_still_says_plan_review`：方案版本结算不匹配，已在未修改的基线工作区独立复现。

本次未重启服务、未执行在线生成、未批准或修改历史现场草稿。审批证据表示后端存在当前有效请求，不证明浏览器已经渲染该卡片。
