# Reference Remix Production Plan Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让参考视频改编从“只生成台词”变为先生成可编辑的整片作品方案，且已有台词可无损转入方案。

**Architecture:** 复用当前工作区已有的 `production_plan.v1` 前后端草稿、产物与编译器。画布入口只负责选择或创建方案节点；纯函数负责旧稿转换、分镜摘要与素材缺项；方案节点负责调用 AI、编辑、创建素材节点和进入编排。旧 `remix_script.v1` 原样保留。

**Tech Stack:** React 19、TypeScript、Zustand、Vitest、FastAPI、Pydantic、pytest。

## Global Constraints

- `production_plan.v1` 是方案唯一事实来源；继续支持已有 `remix_script.v1`，不得静默改写或删除。
- 输出限制沿用现有 1080×1920、30 fps、最长 60 秒。
- 未生成素材不冒充已绑定素材；绑定必须检查媒体类型、哈希、版本与时长。
- 旧稿转换须保留主题、说话人、台词逐字及顺序；AI 不得在补规划时改写锁定台词。
- 无台词段落有效；“出镜口播”“画外旁白”是制作意图，不保证自动口型同步。
- 任何前端视觉变更先遵守根目录 `DESIGN.md`；不改视觉 token。
- 当前 worktree 有其他未提交改动；只暂存本计划涉及的文件，不清理、重置或顺手提交其他变更。

**Preflight:** 在 Task 1 前运行 `pnpm --dir frontend test src/features/canvas/remix/createScriptFromBlueprint.test.ts src/features/canvas/nodes/ProductionPlanNode.test.tsx` 与 `uv run pytest tests/test_production_plan.py -q`。预期现有测试通过；若基线失败，先定位失败与本任务的关系，不把失败算成新改动的回归。

---

## File map

| 文件 | 单一职责 |
| --- | --- |
| `frontend/src/features/canvas/remix/createScriptFromBlueprint.ts` | “开始改编”时选择已有方案/旧稿，或新建方案 |
| `frontend/src/features/canvas/remix/createProductionPlan.ts` | 新建方案及显式转换旧稿的画布连接 |
| `frontend/src/features/canvas/remix/legacyScriptToPlan.ts` | 无损解析旧台词并构造可编辑方案草稿 |
| `src/novelvideo/freezone/remix/production_plan.py` | 约束 AI 草稿保留锁定台词 |
| `src/novelvideo/api/routes/remix.py`、`frontend/src/api/remix.ts` | 传递可选的锁定说话轮次 |
| `frontend/src/features/canvas/remix/productionPlanPresentation.ts` | 每段摘要、A-roll/旁白说明和缺项计算 |
| `frontend/src/features/canvas/remix/createProductionPlanCueNode.ts` | 从画面/声音需求创建可继续制作的节点 |
| `frontend/src/features/canvas/nodes/ProductionPlanNode.tsx` | 方案编辑、摘要、素材动作、错误提示 |
| `frontend/src/features/canvas/remix/RemixScriptPanel.tsx`、`frontend/src/features/canvas/nodes/ScriptNode.tsx` | 旧稿上的显式转换入口 |
| 相邻 `*.test.ts(x)`、`tests/test_production_plan.py`、`tests/test_api_remix_artifacts.py` | 单元、组件和 API 回归 |

## Task 1: 默认进入方案且不重复建节点

**Files:**
- Modify: `frontend/src/features/canvas/remix/createScriptFromBlueprint.ts`
- Modify: `frontend/src/features/canvas/remix/createProductionPlan.ts`
- Test: `frontend/src/features/canvas/remix/createScriptFromBlueprint.test.ts`

**Interfaces:**
- Consumes: `createProductionPlanFromStory(storyId: string): string`。
- Produces: `createScriptFromBlueprint(storyId: string): string`；已有方案优先、否则保留旧稿入口、否则新建方案。

- [ ] **Step 1: 写失败测试。** 在 `createScriptFromBlueprint.test.ts` 增加：无连接脚本时调用后 `addNode` 的 data 含 `remixRole: 'production_plan'` 和 `productionPlanOpenOnCreate: true`；同时存在旧稿与方案时选方案；仅有旧稿时仍打开旧编辑器；连线失败删除新方案节点。

```ts
it('starts with one editable production plan', () => {
  createScriptFromBlueprint('story-1');
  expect(addNode).toHaveBeenCalledWith('scriptNode', expect.anything(), expect.objectContaining({
    remixRole: 'production_plan', productionPlanOpenOnCreate: true,
  }));
  expect(addEdge).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: 确认测试失败。** 运行 `pnpm --dir frontend test src/features/canvas/remix/createScriptFromBlueprint.test.ts`；预期新断言失败，原测试仍可执行。
- [ ] **Step 3: 最小实现。** 让 `createScriptFromBlueprint` 在无连接节点时返回 `createProductionPlanFromStory(storyId)`；候选排序先按 `remixRole === 'production_plan'`，再按现有内容分数。`createProductionPlanFromStory` 继续复制 `referenceBlueprintRef` 并在连线失败时删除刚建节点。不要删旧脚本代码，旧节点仍可打开。

```ts
const existing = connectedScripts.sort((left, right) =>
  Number(right.data.remixRole === 'production_plan') - Number(left.data.remixRole === 'production_plan')
  || scriptContentScore(right.data) - scriptContentScore(left.data))[0];
if (!existing) return createProductionPlanFromStory(storyId);
```

- [ ] **Step 4: 跑聚焦测试。** 同上命令；预期全部 PASS。
- [ ] **Step 5: 只暂存本任务文件并提交。** 提交信息 `feat(remix): open production plan by default`。

## Task 2: 显式转换旧稿，台词不丢

**Files:**
- Create: `frontend/src/features/canvas/remix/legacyScriptToPlan.ts`
- Create: `frontend/src/features/canvas/remix/legacyScriptToPlan.test.ts`
- Modify: `frontend/src/features/canvas/remix/createProductionPlan.ts`
- Modify: `frontend/src/features/canvas/remix/RemixScriptPanel.tsx`
- Modify: `frontend/src/features/canvas/nodes/ScriptNode.tsx`

**Interfaces:**
- Produces: `legacyScriptToPlan(dialogue: string, topic: string): ProductionPlan`；`convertLegacyScriptToPlan(scriptNodeId: string): string`。
- Conversion creates a separate plan node, connects the same story, stores `productionPlanLockedTurns`, and reuses the existing plan if called again.

- [ ] **Step 1: 写失败测试。** 测试 `女：晚上喝咖啡吗？\n男：换成黑咖啡。` 转为两条 `{speaker,text}`，保持问号、标点与顺序；无角色前缀的非空行作为旁白。测试两次转换只新建一次，旧节点不删除。

```ts
expect(legacyScriptToPlan('女：晚上喝咖啡吗？\n男：换成黑咖啡。', '黑咖啡').beats[0].turns)
  .toEqual([{ speaker: '女', text: '晚上喝咖啡吗？' }, { speaker: '男', text: '换成黑咖啡。' }]);
```

- [ ] **Step 2: 确认新测试失败。** 运行 `pnpm --dir frontend test src/features/canvas/remix/legacyScriptToPlan.test.ts`；预期模块不存在。
- [ ] **Step 3: 实现纯转换。** 使用逐行正则 `^\s*([^：:\n]{1,24})[：:]\s*(\S.*)$`；每个非空行生成一个 turn，不能 `trim()` 台词内部字符。一个初始 beat 足以承载旧句，AI 后续按叙事目的重新分段；放入空的主画面需求和与台词一致的声音需求。

```ts
export function legacyScriptToPlan(dialogue: string, topic: string): ProductionPlan {
  const turns = dialogue.split(/\r?\n/).filter((line) => line.trim()).map((line) => {
    const match = /^\s*([^：:\n]{1,24})[：:]\s*(\S.*)$/.exec(line);
    return match ? { speaker: match[1].trim(), text: match[2] }
      : { speaker: '旁白', text: line };
  });
  const plan = emptyProductionPlan();
  plan.goal = topic.trim() || '沿用原改编脚本';
  plan.beats[0].purpose = '保留已有台词，补充整体规划';
  plan.beats[0].turns = turns;
  plan.beats[0].duration_frames = Math.min(1800, Math.max(90, turns.length * 75));
  plan.beats[0].audio = [{ id: crypto.randomUUID(), role: turns.every((turn) => turn.speaker === '旁白') ? 'narration' : 'speech', description: '按已确认台词制作人声', media: null }];
  return plan;
}
```

- [ ] **Step 4: 接旧稿按钮。** `RemixScriptPanel` 新增 `onConvertToPlan?: () => void`，仅旧稿内容非空时显示“转为作品方案”；`ScriptNode` 回调调用 `convertLegacyScriptToPlan(id)` 并关闭旧稿弹窗。转换用旧节点的 `remixTopic`、`remixScriptDraft.dialogue`，不改旧节点数据/产物；已有同源方案时打开它。
- [ ] **Step 5: 跑测试与提交。** 运行新测试及 `pnpm --dir frontend test src/features/canvas/remix/createScriptFromBlueprint.test.ts`；预期 PASS。只暂存本任务文件，提交 `feat(remix): preserve dialogue when converting to plan`。

## Task 3: AI 补规划时锁定旧台词

**Files:**
- Modify: `src/novelvideo/api/routes/remix.py`
- Modify: `src/novelvideo/freezone/remix/production_plan.py`
- Modify: `frontend/src/api/remix.ts`
- Modify: `frontend/src/features/canvas/nodes/ProductionPlanNode.tsx`
- Test: `tests/test_production_plan.py`
- Test: `tests/test_api_remix_artifacts.py`

**Interfaces:**
- `generate_production_plan_draft(*, topic: str, blueprint: dict | None, username: str, locked_turns: list[dict[str, str]] | None = None) -> dict`
- `generateProductionPlanDraft(project, topic, blueprintRef?, analysisContext?, lockedTurns?)` sends `locked_turns` only when nonempty.

- [ ] **Step 1: 写失败测试。** Mock `generate_recipe_text` 返回改写或漏句的 plan，断言函数重试后抛出 `ValueError('锁定台词未原样保留')`；返回一致台词则成功；API 对无效 `locked_turns` 结构返回 422。

```python
assert [turn.model_dump(include={"speaker", "text"}) for beat in plan.beats for turn in beat.turns] == locked_turns
```

- [ ] **Step 2: 确认测试失败。** 运行 `uv run pytest tests/test_production_plan.py tests/test_api_remix_artifacts.py -q`；预期新增测试 FAIL。
- [ ] **Step 3: 实现接口与校验。** `ProductionPlanDraftBody` 增加 `locked_turns: list[dict[str,str]] | None = Field(default=None, max_length=80)`；在调用 AI 前校验每项只有 `speaker`、`text` 且 text 非空。将锁定轮次 JSON 加到模型上下文并在提示词声明“顺序、说话者、台词逐字不可变”；每次 AI 结果通过 `ProductionPlan.model_validate` 后对扁平化 turns 作完全相等比较，失败重试一次，第二次报明确错误。前端仅从转换后保存的 `productionPlanLockedTurns` 发送此字段，当前草稿在请求失败时保持原样。

```python
def _matches_locked_turns(draft: dict[str, Any], locked: list[dict[str, str]]) -> bool:
    actual = [{"speaker": turn.get("speaker", ""), "text": turn.get("text", "")}
              for beat in draft["beats"] for turn in beat.get("turns", [])]
    return actual == locked
```

- [ ] **Step 4: 跑聚焦测试。** 同 Step 2；预期 PASS。
- [ ] **Step 5: 只暂存本任务文件并提交。** 提交 `feat(remix): keep converted dialogue fixed in plan drafts`。

## Task 4: 让方案和 A-roll 一眼可读

**Files:**
- Create: `frontend/src/features/canvas/remix/productionPlanPresentation.ts`
- Create: `frontend/src/features/canvas/remix/productionPlanPresentation.test.ts`
- Modify: `frontend/src/features/canvas/nodes/ProductionPlanNode.tsx`
- Test: `frontend/src/features/canvas/nodes/ProductionPlanNode.test.tsx`

**Interfaces:**
- Produces: `summarizeBeat(beat: PlanBeat, index: number, fps: number): { title: string; duration: string; speechMode: string; speakers: string; visual: string; audio: string }`。
- `missingPlanCues(plan: ProductionPlan): Array<{ beatIndex: number; kind: 'visual' | 'audio'; cueId: string }>` powers visible status and pre-compose block; audio list may be empty.

- [ ] **Step 1: 写失败测试。** 有 `speech` 的 beat 显示“出镜口播”，有 `narration` 的显示“画外旁白”；无台词显示“无台词”；摘要显示画面描述而不只显示 `purpose`；未绑定主画面与声明的声音显示缺项。有台词但没有 `speech`/`narration` 也算缺项；无台词且空 audio 不算缺项。组件测试检查段落卡片中有“谁说 / 看到 / 听到 / 缺少素材”及对应值。

```ts
expect(summarizeBeat(beatWithNarration, 0, 30)).toMatchObject({
  speechMode: '画外旁白', visual: '咖啡杯特写', audio: '男声解释口感',
});
```

- [ ] **Step 2: 确认测试失败。** 运行 `pnpm --dir frontend test src/features/canvas/remix/productionPlanPresentation.test.ts src/features/canvas/nodes/ProductionPlanNode.test.tsx`；预期新测试 FAIL。
- [ ] **Step 3: 实现展示 helper 与卡片。** `summarizeBeat` 从 `beat.turns`、`beat.visuals`、`beat.audio` 计算可读摘要；`ProductionPlanNode` 顶部显示目标、总时长与段落数，卡片先显示摘要，详细输入与段内时序放到展开区。声音 role 选择旁加入说明：`speech` 指计划拍说话者画面，`narration` 指画外声音；不要将整段配音本身称为 A-roll。已有绑定提示继续保留。

```ts
export function missingPlanCues(plan: ProductionPlan) {
  return plan.beats.flatMap((beat, beatIndex) => [
    ...(!beat.visuals.some((cue) => cue.role === 'primary') ? [{ beatIndex, kind: 'visual' as const, cueId: 'missing-primary' }] : []),
    ...beat.visuals.filter((cue) => !cue.media).map((cue) => ({ beatIndex, kind: 'visual' as const, cueId: cue.id })),
    ...(beat.turns.length > 0 && !beat.audio.some((cue) => cue.role === 'speech' || cue.role === 'narration') ? [{ beatIndex, kind: 'audio' as const, cueId: 'missing-voice' }] : []),
    ...beat.audio.filter((cue) => !cue.media).map((cue) => ({ beatIndex, kind: 'audio' as const, cueId: cue.id })),
  ]);
}
```

- [ ] **Step 4: 阻止空缺进入编排。** `createCompose` 在保存前先列出所有未绑定、过期、主画面覆盖不连续的需求，错误如“第 2 段缺主画面素材”；失败时不创建 compose 节点。保留后端 `compile_production_plan` 的最终校验。
- [ ] **Step 5: 跑测试与提交。** 同 Step 2，并跑 `pnpm --dir frontend build`；预期 PASS。只暂存本任务文件，提交 `feat(remix): summarize plan beats and a-roll intent`。

## Task 5: 从方案需求创建素材节点

**Files:**
- Create: `frontend/src/features/canvas/remix/createProductionPlanCueNode.ts`
- Create: `frontend/src/features/canvas/remix/createProductionPlanCueNode.test.ts`
- Modify: `frontend/src/features/canvas/nodes/ProductionPlanNode.tsx`
- Test: `frontend/src/features/canvas/nodes/ProductionPlanNode.test.tsx`

**Interfaces:**
- Produces: `createProductionPlanCueNode(planNodeId: string, beat: PlanBeat, cue: PlanCue, kind: 'visuals' | 'audio'): string`。
- Creates node with `productionPlanCueId`, `productionPlanBeatId`, `prompt`/`description`, and a `node -> plan` edge; media remains `null` until generated or uploaded.

- [ ] **Step 1: 写失败测试。** 点击每个需求旁的“制作素材”后创建且连接一个画面/音频节点，初始提示含目标段落与 cue 描述；重复点击聚焦原节点，不重复创建；连线失败删除刚建节点；计划的 `cue.media` 仍为 null。

```ts
expect(addNode).toHaveBeenCalledWith('videoNode', expect.anything(), expect.objectContaining({
  productionPlanCueId: cue.id, productionPlanBeatId: beat.id,
}));
expect(addEdge).toHaveBeenCalledWith('generated-node', 'plan-1');
```

- [ ] **Step 2: 确认测试失败。** 运行 `pnpm --dir frontend test src/features/canvas/remix/createProductionPlanCueNode.test.ts`；预期模块不存在。
- [ ] **Step 3: 实现创建 helper。** `visuals` 默认创建 `videoNode`，`audio` 创建 `audioNode`，使用 `store.findNodePosition` 与 `store.addNode`、`store.addEdge`；节点数据携带 `prompt: [beat.purpose, cue.description].filter(Boolean).join('；')` 和标识字段，不伪造 `videoUrl`/`audioUrl`。先搜索已有相同 `productionPlanCueId` 与连线再决定复用。
- [ ] **Step 4: 接 UI 和绑定。** `ProductionPlanNode` 在 cue 行增加“制作素材”按钮；生成完成后原有候选列表与 `bind` 逻辑负责显式绑定，状态更新为已绑定，版本变化提示重新绑定。
- [ ] **Step 5: 跑测试与提交。** 运行本任务测试及 `pnpm --dir frontend build`；预期 PASS。只暂存本任务文件，提交 `feat(remix): create media nodes from plan cues`。

## Task 6: 端到端验收与文档

**Files:**
- Modify: `frontend/src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx`
- Modify: `docs/zh/guides/reference-video-remix.md`
- Modify: `docs/production-plan.md`

**Interfaces:** No new API; verifies Tasks 1–5 together.

- [ ] **Step 1: 写旅程测试。** 模拟“原片分析 → 开始改编 → 输入主题 → 生成方案 → 查看段落摘要 → 编辑台词 → 制作/绑定素材 → 进入成片编排”，另测旧脚本显式转换后旧稿仍可打开。
- [ ] **Step 2: 确认测试失败。** 运行 `pnpm --dir frontend test src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx`；预期新增断言 FAIL。
- [ ] **Step 3: 补齐旅程接线与中文指引。** 文档说明先规划、再做画面/声音、最后编排；明确 AI 规划本身不生成口型或素材。只修复本旅程暴露的接线问题，不改其他流程。
- [ ] **Step 4: 全量相关验证。** 运行 `pnpm --dir frontend test src/features/canvas/remix/createScriptFromBlueprint.test.ts src/features/canvas/remix/legacyScriptToPlan.test.ts src/features/canvas/remix/productionPlanPresentation.test.ts src/features/canvas/remix/createProductionPlanCueNode.test.ts src/features/canvas/nodes/ProductionPlanNode.test.tsx src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx`、`pnpm --dir frontend build`、`uv run pytest tests/test_production_plan.py tests/test_api_remix_artifacts.py -q`、`npx @google/design.md lint DESIGN.md`；全部预期 PASS、lint 0 errors。
- [ ] **Step 5: 只暂存本任务文件并提交。** 提交 `docs(remix): explain production plan workflow`；最终 `git status --short` 中不应出现本任务未提交文件，但保留原先其他改动。
