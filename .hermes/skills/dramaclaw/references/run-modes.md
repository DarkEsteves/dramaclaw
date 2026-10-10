# 运行模式与授权边界

本文件是流水线推进与确认规则的唯一来源；playbook 描述业务顺序，不另设每轮写操作数限制。

## 范围与确认

- 只读状态直接查询。用户已明确目标、对象与约束时，不先询问是否列进度。
- 复用会话内已明确的授权、模式、提供商及预算。没有指定模式时，按请求范围推进，不强制询问模式。
- “只启动这个任务”“看进度”限于该任务；“继续”承接已明确的目标，目标确实不明时才问必要问题。
- “完成本集”“自动推进”允许按必要依赖顺序执行至该目标，不扩大到其他集或重做已有有效产物。
- 付费生成必须处于已授权提供商、数量及预算范围；无法从请求或上下文确定时，先完成只读检查，再一次性说明预计范围并确认。不得把大目标当作无限成本授权。
- 重新摄入/覆盖已有成果继续遵守 SKILL.md 的具体影响说明与二次确认。删除、凭据、敏感数据外传、权限修改及发布保留必要授权；不重复询问已经明确批准的同一动作。

## 逐步确认模式

仅在用户要求“每步问我”等模式时启用。执行已批准步骤，下一未批准步骤前暂停；状态读取和该步骤内低风险准备不单独确认。用户更改模式时更新会话偏好。

## 按目标推进

先核对必要状态，以 `pipeline/status.next_step` 和对应步骤文档为准顺序执行。同一项目的冲突写任务串行，依赖完成并通过必要质量门后再继续。不要因为完成一个写操作就强制结束用户授权。

任务观察、去重与失败恢复见 `async-tasks.md`。范围完成、用户要求停止、必要授权缺失、前置素材缺失或不可恢复错误时停止相关工作并说明具体原因。工具无法持续等待时返回真实任务状态和恢复点，不要求用户重新授权已有目标。

本文件只决定执行前是否再次询问用户及已授权范围的推进方式，不增加、删除或重排业务步骤；流程顺序以 playbooks 为准。

## 外围本集自动协调器

当前消息包含 `[DRAMACLAW_RUN_MODE]` 且 `mode=episode_auto` 时，用户已通过外围“本集自动”按钮授权持续推进本集，不再次询问运行模式或逐步确认。此路径由后端协调器在任务完成后持久化触发新的续跑轮次；每轮只执行一个写任务，避免同一目标由宿主和后端重复推进。普通已授权目标仍按上面的范围规则执行。

开启本集自动前核对声线：就绪时不增加询问；缺失时让用户选择匹配系统声线或到“虾塘”上传/录制。该选择保存为 `voice_policy`：`system` 是后续系统声线准备的已有授权，`custom` 则不得替换为系统声线。

一般失败或取消、覆盖、删除、清空、缺少必要选择、结果不完整及最终成片完成时暂停自动协调器。后端明确授权补齐指定角色缺失 Portrait 时只执行这个恢复任务，同一角色最多自动尝试一次；下一轮再恢复身份图，不同时重试多个依赖。

外围虾导不收取 Agent 积分，也不要展示虾画 Agent 积分报价。

### 自动运行中的用户消息

- 用户可以在本集自动运行或任务生成期间继续发消息。
- 问答、解释、查看状态和进度不改变运行模式；后端自动续跑会等待当前聊天轮结束。
- 虾导判断消息可能要求修改、重做、替换或配置变更时，先调用 `dramaclaw_control_episode_auto(action="suspend")` 暂停后续推进，再询问确认；本轮不得执行该修改。
- 用户否认修改、说“算了/保持原样/继续自动”等意思时，调用 `action="resume"` 恢复同一自动会话。
- 用户确认修改时先调用 `action="pause"` 退出自动模式，再按普通单轮写入规则处理最多一个已确认修改；这样修改任务的状态与完成通知继续沿用手动模式。修改完成后再询问是否重新启用本集自动。
- 用户明确要求停止或切换手动时调用 `action="pause"`。
- `suspend`、`resume`、`pause` 都不取消当前 queued/running 任务。只有用户明确确认取消具体任务时，才走任务取消流程。

### 完成数据展示规则

按用户请求范围展示或汇总步骤的真实产物；最终成片遵守 delivery-boundaries.md。媒体类必须调用对应展示工具；文本/列表类用 markdown 表格或简短列表。没有可展示数据时，如实说明“已完成，但当前接口未返回可展示产物”，不要拼 URL、猜路径或拿旧数据充数。

| 步骤 | 完成后读取 / 展示 |
|------|-------------------|
| 摄入 | `dramaclaw_pipeline_status` 汇总 ingested/configured 状态；不要展示原文全文 |
| 配置项目 | `dramaclaw_get(path="/projects/{project}")` 汇总视觉风格、叙事方式、节奏、音频/视频配置 |
| 角色提取 | `dramaclaw_get(path="/projects/{project}/characters")` 展示角色列表 |
| 角色 face_prompt 检查/补齐 | `dramaclaw_get(path="/projects/{project}/characters")` 检查核心角色 `face_prompt`；缺失时先补齐并展示已补角色 |
| 分集规划 | `dramaclaw_get(path="/projects/{project}/episodes")` 展示分集列表 |
| 角色肖像 | `dramaclaw_get_character_media(media_kind="portrait")` 展示肖像 |
| 身份规划 | `dramaclaw_get_character_media(media_kind="identity")` 或角色 identities 接口汇总身份列表；没有身份图时只列身份 |
| 身份图生成 | `dramaclaw_get_character_media(media_kind="identity")` 展示身份图 |
| 脚本生成 | `dramaclaw_get_episode_script(episode=N)` 展示 beat 摘要 |
| 场景规划 | `dramaclaw_get(path="/projects/{project}/scenes")` 展示场景列表 |
| 道具规划 | `dramaclaw_get(path="/projects/{project}/props")` 展示道具列表 |
| 场景参考图 | `dramaclaw_get_scene_images()` 展示场景图 |
| 草图生成 | `dramaclaw_get_sketches(episode=N)` 展示草图 |
| AI 检测 | `dramaclaw_get_episode_script(episode=N)` 或 beats 接口汇总每个 beat 检测到的身份/道具；不要重复调用检测 |
| 全局视频优化 | `dramaclaw_get_episode_script(episode=N)` 或 beats 接口汇总 video_mode / video_prompt 就绪情况 |
| 首帧生成 | `dramaclaw_get_first_frames(episode=N)` 展示首帧 |
| 音频生成 | `dramaclaw_get_episode_media(episode=N, media_type="audio")` 展示音频 |
| 单 beat 视频 | `dramaclaw_get_episode_media(episode=N, media_type="video")` 展示视频片段 |
| 合成导出 | `dramaclaw_get_final_video(episode=N)` 展示最终成片 |
| 最终成片展示 | `dramaclaw_get_final_video(episode=N)`，若不存在则只说明暂无成片 |
