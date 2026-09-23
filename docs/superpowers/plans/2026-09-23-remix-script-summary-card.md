# 改编脚本摘要卡 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将参考改编脚本小节点从整段台词展示改为主题、事件、B-roll 进度、台词预览和下一步组成的摘要卡。

**Architecture:** 继续由 `ScriptNode` 判断参考改编脚本模式，并直接从现有节点数据派生摘要，不增加持久化字段。普通脚本节点继续走原有结果展示分支。

**Tech Stack:** React、TypeScript、Tailwind CSS、Vitest、Testing Library

## Global Constraints

- 仅调整参考改编脚本模式下的小节点内容区。
- 沿用 `DESIGN.md` 和现有 CSS 变量，不新增颜色、圆角或间距变量。
- 节点尺寸、端口、标题、工具条和编辑入口保持不变。
- 没有必需 B-roll 时显示“B-roll 无必需项”。

---

### Task 1: 实现改编脚本摘要卡

**Files:**
- Modify: `frontend/src/features/canvas/nodes/ScriptNode.tsx:472-485,841-854`
- Test: `frontend/src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx`

**Interfaces:**
- Consumes: `data.remixTopic`、`data.remixBrollRequirements`、`remixScriptDraft.selections`、`remixJourneyStatus`。
- Produces: 仅用于 `isRemixScript` 分支的主题、事件数、B-roll 完成度、两至三行台词预览和下一步摘要。

- [ ] **Step 1: 写失败的摘要卡测试**

在 `remixJourneyCanvas.integration.test.tsx` 的改编脚本节点数据中加入主题、两个必需 B-roll 要求和一个已绑定素材的事件，然后断言摘要内容：

```tsx
expect(await screen.findByText('改编主题：新品咖啡')).toBeVisible();
expect(screen.getByText('2 个事件')).toBeVisible();
expect(screen.getByText('B-roll 1/2')).toBeVisible();
expect(screen.getByText('下一步：补齐必需 B-roll')).toBeVisible();
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
cd frontend
pnpm exec vitest run src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx --maxWorkers=1
```

Expected: FAIL，因为现有小节点只显示完整对话和组合状态文本。

- [ ] **Step 3: 计算摘要数据**

在 `ScriptNode.tsx` 的改编脚本状态区增加派生值：

```tsx
const remixRequirements = Array.isArray(data.remixBrollRequirements)
  ? data.remixBrollRequirements as Array<{ event_id: string; required: boolean }>
  : [];
const requiredBroll = remixRequirements.filter((item) => item.required);
const completedRequiredBroll = requiredBroll.filter((requirement) => {
  const event = initialRemixDraft.selections.find((selection) => selection.id === requirement.event_id);
  return Boolean(event?.visualNodeId && event.visualMediaRevision);
}).length;
const remixTopicLabel = typeof data.remixTopic === 'string' && data.remixTopic.trim()
  ? data.remixTopic.trim()
  : '尚未填写改编主题';
```

- [ ] **Step 4: 替换改编脚本内容区**

将完整滚动台词改为固定摘要结构：

```tsx
<div className="nodrag flex min-h-0 flex-1 flex-col gap-2 px-3 py-3 text-xs">
  <p className="truncate font-medium text-text-dark" title={remixTopicLabel}>改编主题：{remixTopicLabel}</p>
  <div className="flex flex-wrap gap-1.5 text-[11px] text-text-muted">
    <span className="rounded-full bg-white/[0.04] px-2 py-1">{initialRemixDraft.selections.length} 个事件</span>
    <span className="rounded-full bg-white/[0.04] px-2 py-1">
      {requiredBroll.length ? `B-roll ${completedRequiredBroll}/${requiredBroll.length}` : 'B-roll 无必需项'}
    </span>
  </div>
  <p className="line-clamp-3 min-h-0 flex-1 whitespace-pre-wrap break-words leading-5 text-text-dark/80">
    {initialRemixDraft.dialogue.trim() || `点击“${remixScriptLabel}”填写台词。`}
  </p>
  <div className="border-t border-white/[0.08] pt-2 text-text-muted">{remixJourneyStatus ?? fallbackStatus}</div>
</div>
```

- [ ] **Step 5: 覆盖无主题和无必需 B-roll 状态**

增加断言：

```tsx
expect(screen.getByText('改编主题：尚未填写改编主题')).toBeVisible();
expect(screen.getByText('B-roll 无必需项')).toBeVisible();
```

- [ ] **Step 6: 运行验证**

Run:

```bash
cd frontend
pnpm exec vitest run src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx src/features/canvas/remix/RemixScriptPanel.test.tsx --maxWorkers=1
pnpm exec tsc -b --pretty false
```

Expected: 所有测试通过且 TypeScript 无错误。

- [ ] **Step 7: 提交实现**

```bash
git add frontend/src/features/canvas/nodes/ScriptNode.tsx frontend/src/features/canvas/remix/remixJourneyCanvas.integration.test.tsx
git commit -m "feat(remix): summarize script canvas node"
```
