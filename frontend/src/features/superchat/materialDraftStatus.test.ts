import { describe, expect, it } from 'vitest';
import i18next from 'i18next';
import { agentRuntimeDisplayPartsForTest, genericToolTitleForTest, toolStatusRuntimeTextForTest } from './superchat-panel';
import { toolStatusPartForTest } from './use-superchat';
import type { ChatMessage } from './types';

describe('material production draft status', () => {
  it.each(['freezone_prepare_material_work', 'freezone_prepare_material_stage'])('labels and preserves delivered %s while hiding duplicate failure', (name) => {
    const failed = { ...toolStatusPartForTest('agent.tool.updated', { type: 'agent.tool.updated', turn_id: 'turn', call_id: 'failed', name: `dramaclaw.${name}`, status: 'failed', input: { operation_id: 'material-operation' }, error: 'connection lost' }, 'turn'), seq: 1 };
    const delivered = { ...toolStatusPartForTest('agent.tool.updated', { type: 'agent.tool.updated', turn_id: 'turn', call_id: 'ready', name: `dramaclaw.${name}`, status: 'completed', input: { operation_id: 'material-operation' }, output: { ok: true, status: 'workflow_draft_ready', draft_id: 'material-draft' } }, 'turn'), seq: 2 };
    const message = (delivered as { event: ChatMessage }).event;
    expect(genericToolTitleForTest(message)).toBe(name === 'freezone_prepare_material_work' ? '规划素材制作' : '准备下一阶段素材');
    const failedMessage = (failed as { event: ChatMessage }).event;
    expect(toolStatusRuntimeTextForTest({ t: i18next.t, status: 'failed', title: genericToolTitleForTest(failedMessage), toolMessage: failedMessage })).toBe('待重新准备素材制作草稿');
    expect(agentRuntimeDisplayPartsForTest([failed, delivered], { streaming: false }).map((part) => part.id)).toEqual(['tool_status:turn:ready']);
  });
});
