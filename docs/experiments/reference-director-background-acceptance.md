# Reference director 后台链路验收

日期：2026-10-10～11。受控集成与下列项目专用 Codex / Chrome 场景已验证。原作品仍等待用户选择复刻方向；独立测试画布的审批不替代原作品的创作确认。

## 受控集成的真实边界

`tests/test_reference_background_journey.py` 在临时目录建立真实私有画布、SQLite continuation/ChatStore、RemixArtifactStore 和 CAS。ASGI 使用固定已认证项目身份解析；分析供应商结果、模型推理、识别供应商和出口计费上下文使用受控替身；分析任务创建、原 task ID 完成栅栏及完成记录使用真实 TaskStateManager SQLite。画布保存、hosted-turn 绑定、保存态读取 API/回执、commission/消息确认、原生方案 API/编辑/审核、素材工作包、持久化工具桥/HTTP callback、媒体 hash、任务 runner 和 operation 推进均使用实际实现。没有调用线上模型、识别服务或付费媒体供应商。

| 场景 | 实际结果 | 仍未证明 |
| --- | --- | --- |
| 分析完成→后台摘要→方向卡 | 原 task 结果先保存到实际分析节点；真实 saved read 标记来源/版本并产生 hosted-turn 回执；模型替身展示摘要，run 到 awaiting_direction。读取一次，无重新分析。 | 当前项目 Codex 真实推理/工具调用。 |
| 真实用户确认→原生方案→审核→工作包 | assistant 的“确认”不能批准；真实 user ChatStore 消息通过宿主入口批准方向/方案，并保留 message ID；原生方案/合成节点、局部编辑、八类审核和上传素材工作包实际保存。工作包 planned/upload 待供素材，不伪报生成。 | 页面点击与供应商真实生成效果。 |
| 方案编辑冲突 | 旧 plan ref 的编辑实际返回 422 `plan changed; reload before editing`；已保存用户段落未被删改。 | 浏览器有未保存内容时 UI 冲突处理，留给实机验证。 |
| 浏览器断开→重连→回执 | 实际 bridge 失败投递保留 pending；新宿主 session 交付原 turn，HTTP context callback 保存实际 current_browser receipt，节点选择来自受控浏览器响应。 | Chrome 网络断开、关闭面板、多标签真实执行；已有专项测试另覆盖这些协议。 |
| 无台词视频 | 本地 FFmpeg 蓝色画面，320×180/24fps/2 秒，无源音轨；实际 compiler/render runner 与 operation needs_confirmation→completed；输出时长/分辨率、静音 PCM 正常，保存阶段观察产生 speech_aligned，无识别调用。源 hash 改变后实际编译拒绝。 | 动作、创意与视觉质量。原生 renderer 添加静音音轨，因此不要求输出文件无音轨。 |
| 同一视频两位发声者 | 离线系统声音 Samantha “Hello”、Daniel “Goodbye” 合入单个实际 MP4，原创短词、无供应商费用；完整源调用受控识别一次；真实 runner 生成两份关联词证据，经 CAS 保存稳定 word ID、两个 ready 句 cue、同一 take；实际原生编译/导出仅一个视频层和一次原声音频，两个片段 PCM 音量均与源接近。 | 实际 ASR 的声学准确率、精确词边界、真人身份/表演质量、听音判断。词时间来自固定源片段夹具范围。非 macOS 无离线 say 时该场景会显式 skip；本机运行未 skip。 |
| 历史假等待恢复 | 真实 ChatStore assistant.message_parts/tool_status.event.raw 保存失败 status/errors/tool_call_status；coordinator list/get 将旧 awaiting_direction 变为可重试 resume_failed，paid task ID 不变。错误 actor state root/turn/canvas/Agent、已有决定、既有成功回执均不误分类。 | 原用户页面能否显示及执行恢复，待主任务实机。 |
| 成功读取后的可选失败 | 保存态/浏览器响应中当前分析的真实摘要阻止旧失败误分类；空 responses 和其他节点读不能冒充成功。 | 可用能力之外的原声/整段运动审查。 |

自动 gate：104 passed、8 条现有依赖弃用警告。相关原有回归：209 passed、12 条现有依赖/FastAPI 生命周期警告。TypeScript `tsc -b` exit 0；工作流契约 760 checks、0 errors、1 条旧 Recipe orphan 提示。三份工作流指南字节相同，现有用户隐藏、禁用与定制优先级测试通过。完整命令与日志索引见 `.cache/runtime-completion/task-6-report.md`。

## 当前 Chrome / 项目 Codex 实机记录

2026-10-11，API8781、前端5173。使用已保存原片及独立 `user_local_runtime_acceptance_*` 测试画布；没有重发分析或素材制作任务。

| 项目 | 实际凭据与结果 | 范围 |
| --- | --- | --- |
| 初始化配置 | 完整 `create_app()` 初始化后 backend=`codex`，二进制 `/Users/wwq/worknewnew/dramaclaw/runtime/codex-0.149.0-src/codex-rs/target/release/codex`，实测 `codex-cli 0.149.0`；前端进程 `VITE_API_URL=http://127.0.0.1:8781`，实际恢复/工具请求在8781返回200。 | 项目要求的鉴权补丁二进制；没有改用全局Codex。 |
| 原分析续跑 | 原run `89297d1c86c9ba5caeef8f3de399773a3da70daa3cf4c229cec3532ab07c93d7`、task `34cbbf32-60e5-4371-951e-e49b57bba09c`、9事件保持；实际Codex resume2=`01a1269b-6ade-7121-9180-54f12e800083` 展示摘要，状态 `awaiting_direction`，`execution_blocker=null`，目标分析来源凭据为 `host_saved_snapshot`。 | 实际受保护 evidence 工具成功；没有重新分析。原用户尚未选择方向，未代其确认。 |
| 原生方案读写 | 独立测试canvas `user_local_runtime_acceptance_474d38d75f`、turn `turn-1791649275300-vwtdjj`：`freezone_get_node_detail` 完成且 `source/read_consistency=current_browser`；`freezone_get_production_state` ready。实际单项 `set_goal` 经页面审批执行，plan `49d04307bed4497cb992cd299f412dac` revision1→2，目标变为“后台恢复验收已编辑”；1段、48帧、2节点保持，canvas revision3。 | 实际浏览器→MCP校验→原生方案/CAS→审批/回执；不是原用户的复刻方向确认或供应商制作。 |
| 刷新重连 | 同一测试页面刷新后恢复聊天、执行结果及plan revision2；后端仍只有1个用户消息、1个assistant消息；同一目标与2节点保持。native mutation回执为1项，重连没有新方案版本。 | 真实刷新恢复；跨worker、待回答卡、超时与旧alias转移另由真实持久化专项测试覆盖，不宣称多机实测。 |
| 本地草稿冲突 | 第一独立测试canvas `user_local_runtime_acceptance_9aae8ec175`：本地缓存草稿存在时服务端测试包版本变化，页面显示“画布保存冲突 / 本地未保存修改已暂存到浏览器”，保留本地内容并阻止自动覆盖。后续使用另一测试canvas继续编辑。 | 冲突由测试包初始化修正触发；未修改原用户内容或丢弃其草稿。原生旧ref拒绝覆盖另有真实CAS测试。 |

首次实机resume1暴露 Codex 仅通过 `done` 交付最终正文，而旧判定只消费 `assistant_message` 的误报；已经通过真实 service→Codex adapter→ChatStore 回归修复，并由上述resume2复验。额外的HTTP恢复乐观消息重复显示已按原turn/actor/scope修复；ChatStore没有重复提交，新增真实hook回归验证重复文本的不同turn仍保留。

当前主页面：`http://127.0.0.1:5173/projects/01M4JWFFHENHAW7X80KJT25CNY/freezone?canvas=user_local_17cvc3s`。API重启期间页面曾回到项目首页，重新打开原画布后恢复；稳定连接下刷新保留地址与数据。没有把服务重启中的导航行为描述成已修复。

截图保存在本机 `/tmp/dramaclaw-page-acceptance/runtime-completion-summary.jpg` 与 `/tmp/dramaclaw-page-acceptance/runtime-completion-native-plan.jpg`；工具来源、保存ref及任务身份通过实际持久化记录核对，截图不是唯一证据。未记录凭证、签名URL、executor token或整段私人原文。

## 最终回归与审查

- 汇总回归：608 Python / 279前端测试通过；汇总后跨模块修补覆盖438 Python、真实Codex交付185及服务189测试通过；最终消息合并231前端测试通过。各命令有范围重叠，不能相加当作唯一测试数。
- TypeScript、Vite生产构建、760项工作流契约（0errors）通过；gitleaks及diff/Ruff检查通过。第三方弃用、Node localstorage、Vite大chunk和1项旧Recipe可达性提示仍已记录。
- 最终独立审查 `final-review.md` 为spec PASS / quality Approved；原生浏览器来源、隔离恢复、历史扫描、真实Codex交付的阻塞发现均已修复。非阻塞维护项：清理已完成的send-origin身份条目，避免长页面生命周期积累；不影响当前已验证的去重/权限语义。

## 品质与部署限制

付费供应商整片制作为 **未验证**。实际供应商制作只在已有真实生成授权范围内进行；本次联调不产生额外素材制作费用。当前自动结构、时长、hash 和 PCM 检查不能证明实际完整台词、发音、嘴形、运动与整片声画质量。音频审听/连续运动自动判断缺失项继续显式交给用户审看。

同一 home node 的共享状态目录、多 worker 桥接/调度由专项受控测试验证；多主机消息与真实线上 QPS/容量未验证。原用户内容、私有范围、项目身份和原任务保留；不清理历史节点，不安装或覆盖用户系统 Skill。

## 2026-10-12：作品方案后台起草回归

本次修复针对方案起草已提交、Agent 工具等待超时、后台任务完成后画布和对话状态不一致的问题。当前方案按制作分支绑定到已确认的方案及合成节点；重复准备或确认会返回既有回执，选错节点会在提交生成前拒绝。原生起草收到真实 workflow run、operation、task 身份后返回 `accepted`，模型生成留在后台。任务终态由后端结算并写回原节点，浏览器恢复时只读取并合并服务端画布；系统观察者按持久化结果通知绑定的 Agent。手动画布操作使用同一个节点 action 和后台结算路径；用户方向仍通过普通对话理解，手动确认仍由既有确认卡执行。

受控测试覆盖成功发布、模型失败、依赖动作阻断、浏览器不读取结果时的结算、重复回调、节点删除或用户编辑后的冲突保留、独立手动任务不续跑 Agent，以及提交回执身份校验。对话发起的方案起草在 workflow 创建前核对持久化 bridge 命令、操作者、画布、指定节点和制作分支；多分支无法唯一对应时拒绝提交。后台发布前再次核对当前分析边、方案绑定和制作停止代次，旧任务只保留冲突记录。后端还记录模型请求、解析、校验、产物保存、节点发布和通知阶段的关联日志；超时仍保留实际异常，不推断供应商原因。

验证范围：本轮 740 个相关 Python 测试、316 个相关前端测试通过；TypeScript 类型检查、关键 Python 文件 Ruff 和 `git diff --check` 通过；工作流契约检查为 760 checks、0 errors，另有既有 Recipe 可达性提示。尚未对用户当前浏览器页面做真实模型和整片视频验收，也未对历史失败现场执行数据恢复或付费重试。

## 2026-10-13：画布交付与对话续接修复

本轮只做代码及本地受控回归，未重启当前浏览器使用的服务，未在用户画布提交新任务。已补充首次画布命令回执日志、保存图结构核对、同事务方案槽位绑定和乱序回执恢复。图已保存但起草 action 尚在运行或失败时，草稿保留为已交付；没有明确零副作用的取消回执时，桥接或确认任务异常不会把同一次提交释放成可重复创建。确认任务终态仍未见图时，制作阶段投影给出 `delivery_unverified`，后台观察者通知核对该次提交。

起草生成约束由正式作品方案模型派生，非法 `provenance` 字段会在保存前被拒绝。普通聊天中的参数回答只在已验证浏览器回执携带当前制作草稿 ID、版本，并匹配当前 commission 及需求摘要时共享给续接；其他工作流在同画布提交的参数卡不能串入。方案准备后 Agent 按当前用户授权在同一轮调用现有画布审批入口；独立“待确认的工作流方案”卡片及其轮询已移除。已提交的草稿仍可由原任务回执恢复，不会因为不再是 `ready` 而被判作未交付。

新增长链回归覆盖回执先到、画布后保存、重复回执、精确节点绑定和后续 action 失败。限定 Python 测试 1014 passed；前端审批相关测试 485 passed；TypeScript `tsc -b` 与 `git diff --check` 通过。一次合并运行中有两项与草图候选插件全局测试替身相互影响的断言失败；这两项单独重跑均通过，未纳入上述 1014 项。当前环境没有 `pre-commit` / `gitleaks` 命令，密钥扫描未执行。真实浏览器手动与自动审批、模型起草、付费媒体任务和用户旧失败现场恢复仍待实机验证。

代码复核补齐取消回执和已保存节点的竞态保护、外部源节点存在性检查，以及确认任务结束后由制作观察器恢复晚到的画布保存。前端不再因工作流节点已经存在而跳过同次审批中缺失的连线或 action；重放复用节点并返回实际 `createdNodeId`，工作流 run 仍使用原 bridge 幂等键。参数回执由普通聊天与后台续接共同读取；需求只改一个字段时，根据回执中的需求快照保留未受影响的已确认字段，模型变动会重新检查该模型相关的字段。旧回执没有需求快照时只在原完整摘要相同时复用。追加的限定回归为 467 个 Python、485 个前端测试通过，TypeScript 类型检查、Ruff 和差异空白检查通过；增量修改后的参数专项测试也通过。未进行真实浏览器或付费生成验收。

最终复核还修复了四个续接边界：普通聊天的参数卡必须在唤醒 Agent 前持久化，失败时不得继续唤醒；参数记录只归属当前仍在执行的 commission；同一个 bridge 的部分工作流重放只允许省略服务端已完成的 action；旧 runner 租约未到期时不得接管，以免重复触发生成。相同方向文字重新确认后，新的 `direction_revision` 也会使旧参数卡失效，commission 产物版本自身前进则不会无故使当前确认失效。最终相关范围的较大 Python 回归为 1137 passed；此后补充方向版本回归，限定 132 passed、8 条第三方弃用警告；前端 485 passed、TypeScript `tsc -b`、Ruff 和 `git diff --check` 通过。没有重启服务或执行用户浏览器中的真实制作，浏览器行为和付费任务仍待单独验收。

## 2026-10-13：已审核作品方案的对话确认衔接

本轮把真实 `production_review.v1` 审核通过、commission 阶段推进和内部待确认方案登记接到同一共享服务。过早调用方案确认工具仍会被拒绝；只有同一画布、操作者、commission、方案版本和方向版本的正式审核已保存，后续回执才会解除这一前置条件错误。Codex 与 Hermes 均使用服务端保存的审核及决定核验，不依赖 Agent 声称已审核。

方案确认通过普通 assistant 回复交付，ChatStore 在同次保存中绑定当前方案、审核和内部决定的精确引用。审核发生在当前 Agent 回合内，或由后台续跑交付，均使用这一关联。只有此前存在对应的已保存回复，真实用户消息才进入自然语言意图解释；提问、含糊回复和修改要求不直接批准未修改的方案。方向与方案的独立确认卡不再发布或显示，原生画布命令及节点 action 审批保持原路径。

临时项目回归覆盖审核重复提交、中断恢复、过早拒绝后的真实结算、跨后端普通回复、持久化展示和真实用户消息批准；方案版本及画布在对话批准期间保持不变。限定测试：199 个 Python、83 个前端通过；TypeScript `tsc -b`、相关 Ruff 与 `git diff --check` 通过。另行扩展到此前未纳入计划的 `test_reference_director_journey.py` 时，255 个通过、3 个失败：测试夹具重复创建同 ID 工作流草稿、缺失 `canvas_id` 的人工 run、以及旧断言预期处理阶段只做一次画布栅栏检查。这三项不在本轮方案确认路径内，未修改业务逻辑来满足它们。未重启服务、未在真实浏览器验证新代码、未恢复旧现场，也未调用付费生成。
