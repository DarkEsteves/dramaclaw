import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { CanvasNode } from '@/features/canvas/domain/canvasNodes';
import type { PlanMediaCandidate } from '@/features/canvas/remix/productionPlanReadiness';
import { ProductionPlanMediaPicker } from './ProductionPlanMediaPicker';

const candidates = [
  { node: { id: 'image-1', type: 'imageGenNode', data: { imageUrl: '/coffee.png' } } as CanvasNode,
    kind: 'image', label: '咖啡主画面', currentPath: 'images/coffee.png', media: null },
  { node: { id: 'audio-1', type: 'audioNode', data: { audioUrl: '/voice.mp3' } } as CanvasNode,
    kind: 'audio', label: '旁白声音', currentPath: 'audio/voice.mp3', media: null },
] as PlanMediaCandidate[];

describe('ProductionPlanMediaPicker', () => {
  it('filters visual candidates and selects from a preview card', () => {
    const onSelect = vi.fn();
    render(<ProductionPlanMediaPicker kind="visuals" description="咖啡特写" candidates={candidates}
      onSelect={onSelect} onClose={vi.fn()} onCreate={vi.fn()} />);
    expect(screen.getByRole('img', { name: '咖啡主画面' })).toHaveAttribute('src', '/coffee.png');
    expect(screen.queryByText('旁白声音')).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('搜索画布素材'), { target: { value: '没有' } });
    expect(screen.queryByText('咖啡主画面')).not.toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText('搜索画布素材'), { target: { value: '咖啡' } });
    fireEvent.click(screen.getByRole('button', { name: '绑定 咖啡主画面' }));
    expect(onSelect).toHaveBeenCalledWith('image-1');
  });

  it('offers a sound node when no existing audio is suitable', () => {
    const onCreate = vi.fn();
    render(<ProductionPlanMediaPicker kind="audio" description="采访者人声" candidates={[]}
      onSelect={vi.fn()} onClose={vi.fn()} onCreate={onCreate} />);
    expect(screen.getByText('画布上还没有可用的声音素材')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '创建声音节点' }));
    expect(onCreate).toHaveBeenCalledWith('audio');
  });

  it('puts the recently used material first for reuse', () => {
    const another = { ...candidates[0], node: { ...candidates[0].node, id: 'image-2' }, label: '另一个画面' };
    render(<ProductionPlanMediaPicker kind="visuals" description="补充镜头" candidates={[candidates[0], another]}
      recentNodeId="image-2" onSelect={vi.fn()} onClose={vi.fn()} onCreate={vi.fn()} />);
    const buttons = screen.getAllByRole('button', { name: /^绑定 / });
    expect(buttons[0]).toHaveAccessibleName('绑定 另一个画面');
  });
});
