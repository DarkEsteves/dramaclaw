import { useEffect, useRef, useState } from 'react';
import { attachRemixMedia, uploadRemixMedia } from '@/api/remix';
import type { UploadedRemixMedia } from './workbenchModel';
import { projectMediaPathFromUrl } from './remixMediaPath';
import { CANVAS_NODE_OPS_PANEL_CLASS } from '@/features/canvas/ui/nodeFrameStyles';

type VideoRole = 'reference' | 'visual' | 'background' | 'speech_clock';
type AudioRole = 'narration' | 'bgm' | 'sfx';

interface Props {
  project: string;
  kind: 'video' | 'audio';
  media?: UploadedRemixMedia;
  sourceUrl?: string | null;
  role: VideoRole | AudioRole;
  onChange: (value: { media: UploadedRemixMedia; role: VideoRole | AudioRole }) => void;
}

const videoRoles: Array<{ value: VideoRole; label: string }> = [
  { value: 'reference', label: '待分析原片' },
  { value: 'background', label: '成片背景 / A-roll' },
  { value: 'visual', label: '替换画面' },
  { value: 'speech_clock', label: '视频原声' },
];
const audioRoles: Array<{ value: AudioRole; label: string }> = [
  { value: 'narration', label: '旁白' },
  { value: 'bgm', label: '背景音乐' },
  { value: 'sfx', label: '音效' },
];

export function RemixMediaInput({ project, kind, media, sourceUrl, role, onChange }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const attachingPath = useRef<string | null>(null);
  const onChangeRef = useRef(onChange);
  const roleRef = useRef(role);
  onChangeRef.current = onChange;
  roleRef.current = role;
  const roles = kind === 'video' ? videoRoles : audioRoles;
  useEffect(() => {
    if (media || !sourceUrl || !project) return;
    const path = projectMediaPathFromUrl(project, sourceUrl);
    if (!path || attachingPath.current === path) return;
    attachingPath.current = path;
    setBusy(true);
    attachRemixMedia(project, path).then((attached) => {
      if (attached.media_kind === kind) {
        setBusy(false);
        onChangeRef.current({ media: attached, role: roleRef.current });
      }
      else setError('当前节点素材类型不匹配');
    }).catch((cause) => {
      setError(cause instanceof Error ? cause.message : '无法引用当前节点素材');
    }).finally(() => { setBusy(false); });
  }, [project, kind, media, sourceUrl]);
  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const uploaded = await uploadRemixMedia(project, file);
      if (uploaded.media_kind !== kind) throw new Error(kind === 'video' ? '请选择视频文件' : '请选择音频文件');
      onChange({ media: uploaded, role });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  };
  return <div className={`nodrag rounded-[12px] p-3 text-xs text-text-dark ${CANVAS_NODE_OPS_PANEL_CLASS}`} onClick={(event) => event.stopPropagation()}>
    <div className="mb-3 text-[12px] font-medium">{kind === 'video' ? '视频用途' : sourceUrl || media ? '音频用途' : '上传音频'}</div>
    <label className="block text-text-muted">{kind === 'video' ? '更换视频文件' : sourceUrl || media ? '更换音频文件' : '选择音频文件'}
      <input aria-label={kind === 'video' ? '更换视频文件' : sourceUrl || media ? '更换音频文件' : '选择音频文件'} type="file" accept={kind === 'video' ? 'video/*' : 'audio/*'} disabled={busy} className="nodrag mt-1 block w-full rounded-[12px] border border-white/10 bg-surface-field px-2 py-1.5 text-[11px] text-text-dark file:mr-2 file:rounded-md file:border-0 file:bg-white/10 file:px-2 file:py-1 file:text-text-dark disabled:opacity-50" onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file); }} />
    </label>
    <label className="mt-3 block text-text-muted">用途
      <select aria-label="素材用途" value={role} disabled={busy} className="nodrag mt-1 h-9 w-full rounded-[12px] border border-white/10 bg-surface-field px-3 text-xs text-text-dark" onChange={(event) => {
        if (media) onChange({ media, role: event.target.value as VideoRole | AudioRole });
      }}>
        {roles.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select>
    </label>
    {media && <div className="mt-2 text-text-muted" title={media.media_path}>
      <div className="truncate">{media.media_path.split('/').pop()}</div>
      <div>{(media.duration_us / 1_000_000).toFixed(1)} 秒{media.width_px && media.height_px ? ` · ${media.width_px}×${media.height_px}` : ''}{media.fps_num && media.fps_den ? ` · ${(media.fps_num / media.fps_den).toFixed(2)} fps` : ''} · {media.audio_stream_count ?? (media.has_audio ? 1 : 0)} 条音轨</div>
    </div>}
    {busy && <div role="status">正在读取素材…</div>}
    {error && <div role="alert" className="text-destructive">{error}</div>}
  </div>;
}
