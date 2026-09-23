export interface SemanticTimelineToolbarData {
  compileOperationId?: unknown;
}

export function semanticTimelineToolbarState(data: SemanticTimelineToolbarData): {
  label: '更新时间区间' | '计算中…';
  disabled: boolean;
  title: string;
} {
  const busy = typeof data.compileOperationId === 'string' && data.compileOperationId.length > 0;
  return busy
    ? { label: '计算中…', disabled: true, title: '正在重新计算每个事件的起止帧' }
    : { label: '更新时间区间', disabled: false, title: '根据当前脚本、语音对齐和节点内调整，重新计算每个事件的起止帧' };
}
