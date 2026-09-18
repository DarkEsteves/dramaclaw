import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const { uploadRemixMedia } = vi.hoisted(() => ({ uploadRemixMedia: vi.fn() }));
vi.mock('@/api/remix', () => ({ uploadRemixMedia }));

import { RemixMediaInput } from './RemixMediaInput';

describe('RemixMediaInput', () => {
  it('uploads project media and publishes its role and exact media reference', async () => {
    const media = { media_path: 'input/clip.mp4', media_hash: 'a'.repeat(64), url: '/clip.mp4', size: 2, media_kind: 'video' as const, duration_us: 2_000_000, has_audio: true };
    uploadRemixMedia.mockResolvedValue(media);
    const onChange = vi.fn();
    render(<RemixMediaInput project="demo" kind="video" role="reference" onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('改编视频素材'), { target: { files: [new File(['video'], 'clip.mp4', { type: 'video/mp4' })] } });
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ media, role: 'reference' }));
  });
});
