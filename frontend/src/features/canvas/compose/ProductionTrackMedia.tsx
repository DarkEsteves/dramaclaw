import { useEffect, useRef, useState } from 'react';
import { getFilmstrip, pickFrame, type FilmstripFrame } from './filmstrip';
import { getCachedAudioPeaks, loadAudioPeaks, PEAK_BUCKETS_PER_SEC } from './audioPeaks';

interface Props {
  kind: 'image' | 'video' | 'audio';
  url: string;
  sourceInMs: number;
  sourceOutMs: number;
  width: number;
  durationMs: number;
}

/** Preview only visible timeline media; both decoders cache by source URL. */
export function ProductionTrackMedia({ kind, url, sourceInMs, sourceOutMs, width, durationMs }: Props) {
  const root = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [visible, setVisible] = useState(() => typeof IntersectionObserver === 'undefined');
  const [frames, setFrames] = useState<FilmstripFrame[]>([]);
  const [peaks, setPeaks] = useState<Float32Array | null>(() => getCachedAudioPeaks(url));

  useEffect(() => {
    if (visible || !root.current || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: '240px' });
    observer.observe(root.current);
    return () => observer.disconnect();
  }, [visible]);

  useEffect(() => {
    if (!visible || kind !== 'video') return;
    let cancelled = false;
    setFrames([]);
    void getFilmstrip(url).then((result) => { if (!cancelled) setFrames(result); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [visible, kind, url]);

  useEffect(() => {
    // Browser decodeAudioData loads the entire file; long sources keep their track label.
    if (!visible || kind !== 'audio' || durationMs > 600_000) return;
    let cancelled = false;
    setPeaks(getCachedAudioPeaks(url));
    void loadAudioPeaks(url).then((result) => { if (!cancelled) setPeaks(result); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [visible, kind, url, durationMs]);

  useEffect(() => {
    const element = canvas.current;
    if (!visible || !element || !peaks?.length || kind !== 'audio') return;
    // A long placement can span tens of thousands of CSS pixels; bound canvas memory.
    const displayWidth = Math.max(1, Math.min(2048, Math.round(width)));
    const displayHeight = 32;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    element.width = Math.round(displayWidth * ratio);
    element.height = Math.round(displayHeight * ratio);
    const ctx = element.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, displayWidth, displayHeight);
    ctx.fillStyle = 'rgba(235, 248, 255, 0.68)';
    const first = sourceInMs / 1000 * PEAK_BUCKETS_PER_SEC;
    const span = Math.max(1, (sourceOutMs - sourceInMs) / 1000 * PEAK_BUCKETS_PER_SEC);
    for (let x = 0; x < displayWidth; x += 2) {
      const begin = Math.floor(first + x / displayWidth * span);
      const end = Math.max(begin + 1, Math.ceil(first + (x + 2) / displayWidth * span));
      let peak = 0;
      for (let bucket = begin; bucket < end; bucket++) peak = Math.max(peak, peaks[bucket] ?? 0);
      const height = Math.max(1, peak * 13);
      ctx.fillRect(x, 16 - height, 1, height * 2);
    }
  }, [visible, peaks, kind, width, sourceInMs, sourceOutMs]);

  const slots = Math.max(1, Math.min(24, Math.ceil(width / 72)));

  return <div ref={root} aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden rounded-[inherit] opacity-75">
    {visible && kind === 'image' && <img src={url} alt="" className="h-full w-full object-cover" draggable={false} />}
    {visible && kind === 'video' && frames.length > 0 && <div className="flex h-full w-full">{Array.from({ length: slots }, (_, index) => {
      const frame = pickFrame(frames, sourceInMs + (index + 0.5) / slots * Math.max(1, sourceOutMs - sourceInMs));
      return <div key={index} className="h-full min-w-0 flex-1">{frame && <img src={frame.url} alt="" className="h-full w-full object-cover" draggable={false} />}</div>;
    })}</div>}
    {visible && kind === 'audio' && <canvas ref={canvas} className="h-full w-full" />}
  </div>;
}
