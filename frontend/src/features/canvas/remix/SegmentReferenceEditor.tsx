import type { RemixReferenceBinding } from './remixScriptModel';
import { NODE_CONTEXT_CONTROL_TRIGGER_CLASS } from '@/features/canvas/ui/nodeControlStyles';

export interface ReferenceMediaNode {
  id: string;
  label: string;
  kind: 'image' | 'video' | 'audio';
}

const fieldClass = 'nodrag mt-1 w-full rounded-[12px] border border-white/10 bg-surface-field px-3 py-2 text-text-dark outline-none focus:border-white/25';
const kindLabel = { image: '图片', video: '视频', audio: '音频' };

interface Props {
  bindings: RemixReferenceBinding[];
  nodes: ReferenceMediaNode[];
  onChange: (bindings: RemixReferenceBinding[]) => void;
  onApply?: (index: number) => void;
}

export function SegmentReferenceEditor({ bindings, nodes, onChange, onApply }: Props) {
  const available = nodes.filter((node) => !bindings.some((binding) => binding.nodeId === node.id));
  return <div className="mt-2 space-y-2">
    {!bindings.length && <p className="text-text-muted">未使用参考素材</p>}
    {bindings.map((binding, index) => <div key={`${binding.nodeId}-${index}`} className="grid gap-2 rounded-[8px] border border-white/[0.08] bg-white/[0.03] p-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
      <label>素材<select aria-label={`参考 ${index + 1} 素材`} value={binding.nodeId} onChange={(event) => onChange(bindings.map((item, position) => position === index ? { ...item, nodeId: event.target.value } : item))} className={fieldClass}>
        {!nodes.some((node) => node.id === binding.nodeId) && <option value={binding.nodeId}>素材已移除 · {binding.nodeId}</option>}
        {nodes.filter((node) => node.id === binding.nodeId || !bindings.some((item) => item.nodeId === node.id)).map((node) => <option key={node.id} value={node.id}>{kindLabel[node.kind]} · {node.label}</option>)}
      </select></label>
      <label>用途（可留空）<input aria-label={`参考 ${index + 1} 用途`} value={binding.purpose} onChange={(event) => onChange(bindings.map((item, position) => position === index ? { ...item, purpose: event.target.value } : item))} placeholder="可选，例如保持镜头运动" className={fieldClass} /></label>
      <div className="flex gap-1">
        {onApply && <button type="button" aria-label={`将参考 ${index + 1} 应用到其他段落`} onClick={() => onApply(index)} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>批量应用</button>}
        <button type="button" aria-label={`移除参考 ${index + 1}`} onClick={() => onChange(bindings.filter((_, position) => position !== index))} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>移除</button>
      </div>
    </div>)}
    <button type="button" disabled={!available.length} onClick={() => onChange([...bindings, { nodeId: available[0].id, purpose: '' }])} className={NODE_CONTEXT_CONTROL_TRIGGER_CLASS}>添加参考素材</button>
  </div>;
}
