import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const { uploadRemixMedia, attachRemixMedia } = vi.hoisted(() => ({ uploadRemixMedia: vi.fn(), attachRemixMedia: vi.fn() }));
vi.mock('@/api/remix', () => ({ uploadRemixMedia, attachRemixMedia }));

import { RemixMediaInput } from './RemixMediaInput';

describe('RemixMediaInput', () => {
  it('uploads project media and publishes its role and exact media reference', async () => {
    const media = { media_path: 'input/clip.mp4', media_hash: 'a'.repeat(64), url: '/clip.mp4', size: 2, media_kind: 'video' as const, duration_us: 2_000_000, has_audio: true };
    uploadRemixMedia.mockResolvedValue(media);
    const onChange = vi.fn();
    render(<RemixMediaInput project="demo" kind="video" role="reference" onChange={onChange} />);
    fireEvent.change(screen.getByLabelText('更换视频文件'), { target: { files: [new File(['video'], 'clip.mp4', { type: 'video/mp4' })] } });
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ media, role: 'reference' }));
  });

  it('uses media already present in the video node', async () => {
    const media = { media_path: 'freezone/_uploads/clip.mp4', media_hash: 'b'.repeat(64), url: '/clip.mp4', size: 2, media_kind: 'video' as const, duration_us: 2_000_000, has_audio: true };
    attachRemixMedia.mockResolvedValue(media);
    const onChange = vi.fn();
    const { rerender } = render(<RemixMediaInput project="demo" kind="video" role="reference" sourceUrl="/static/projects/demo/freezone/_uploads/clip.mp4?v=1#t=0.1" onChange={onChange} />);
    await waitFor(() => expect(attachRemixMedia).toHaveBeenCalledWith('demo', 'freezone/_uploads/clip.mp4'));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ media, role: 'reference' }));
    rerender(<RemixMediaInput project="demo" kind="video" role="reference" media={media} sourceUrl="/static/projects/demo/freezone/_uploads/clip.mp4" onChange={onChange} />);
    expect(screen.queryByText('正在读取素材…')).not.toBeInTheDocument();
    expect(screen.getByText(/2\.0 秒/)).toBeInTheDocument();
  });
});
