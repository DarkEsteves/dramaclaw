import { describe, expect, it } from 'vitest';
import { semanticTimelineToolbarState } from './semanticTimelineToolbarState';

describe('semantic timeline toolbar state', () => {
  it('explains that the action updates event time ranges', () => {
    expect(semanticTimelineToolbarState({})).toEqual({
      label: '按实际素材排时间',
      disabled: false,
      title: '根据当前脚本、语音对齐和节点内调整，重新计算每个事件的起止帧',
    });
  });

  it('shows visible progress while compilation is running', () => {
    expect(semanticTimelineToolbarState({ compileOperationId: 'operation-1' })).toEqual({
      label: '排时间中…',
      disabled: true,
      title: '正在重新计算每个事件的起止帧',
    });
  });
});
