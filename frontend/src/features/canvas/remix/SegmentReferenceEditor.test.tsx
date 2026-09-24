import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SegmentReferenceEditor } from './SegmentReferenceEditor';

describe('SegmentReferenceEditor', () => {
  const nodes = [
    { id: 'portrait', label: '角色照片', kind: 'image' as const },
    { id: 'shot', label: '原片镜头', kind: 'video' as const },
    { id: 'voice', label: '声音样本', kind: 'audio' as const },
  ];

  it('starts empty and lets the user choose any media type and state its purpose', () => {
    const onChange = vi.fn();
    render(<SegmentReferenceEditor bindings={[]} nodes={nodes} onChange={onChange} />);
    expect(screen.getByText('未使用参考素材')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '添加参考素材' }));
    expect(onChange).toHaveBeenCalledWith([{ nodeId: 'portrait', purpose: '参考画面内容' }]);
  });

  it('shows legacy bindings and permits changing purpose or removing one', () => {
    const onChange = vi.fn();
    render(<SegmentReferenceEditor bindings={[{ nodeId: 'voice', purpose: '参考说话音色' }]} nodes={nodes} onChange={onChange} />);
    expect(screen.getByRole('option', { name: /声音样本/ })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('参考 1 用途'), { target: { value: '参考语速' } });
    expect(onChange).toHaveBeenCalledWith([{ nodeId: 'voice', purpose: '参考语速' }]);
    fireEvent.click(screen.getByRole('button', { name: '移除参考 1' }));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it('shows a missing bound node so the user can replace it', () => {
    render(<SegmentReferenceEditor bindings={[{ nodeId: 'deleted', purpose: '参考构图' }]} nodes={nodes} onChange={() => {}} />);
    expect(screen.getByRole('option', { name: '素材已移除 · deleted' })).toBeInTheDocument();
  });

  it('exposes a batch action for each individual binding', () => {
    const onApply = vi.fn();
    render(<SegmentReferenceEditor bindings={[{ nodeId: 'shot', purpose: '参考镜头' }, { nodeId: 'voice', purpose: '参考音色' }]}
      nodes={nodes} onChange={() => {}} onApply={onApply} />);
    fireEvent.click(screen.getByRole('button', { name: '将参考 2 应用到其他段落' }));
    expect(onApply).toHaveBeenCalledExactlyOnceWith(1);
  });
});
