import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { VoiceSelectionModal } from '@/features/canvas/nodes/VoiceSelectionModal';

vi.mock('@/api/ops', () => ({
  fetchFreezoneAudioReferences: vi.fn(async () => ({ available: [] })),
  createFreezoneAudioVoice: vi.fn(),
}));

describe('reserved voice design tab', () => {
  it('keeps a role-specific draft editable but never submits while the model is unavailable', () => {
    const onDesignDraftChange = vi.fn();
    render(<VoiceSelectionModal
      open
      initialTab="design"
      currentRef={{ scope: 'project_narrator' }}
      onClose={() => {}}
      onPick={() => {}}
      designRole="男"
      designDraft={{ name: '访谈男声', description: '', previewText: '今天聊什么？' }}
      onDesignDraftChange={onDesignDraftChange}
    />);

    expect(screen.getByText('为男设计音色')).toBeTruthy();
    fireEvent.change(screen.getByRole('textbox', { name: '声音描述' }), {
      target: { value: '年轻、清亮、好奇的访谈主持人声音' },
    });
    expect(onDesignDraftChange).toHaveBeenCalledWith({
      name: '访谈男声',
      description: '年轻、清亮、好奇的访谈主持人声音',
      previewText: '今天聊什么？',
    });
    expect(screen.getByRole('button', { name: '生成试听 · 模型未开放' })).toHaveProperty('disabled', true);
  });
});
