import type { CSSProperties } from 'react';
import type { ProjectedPlacement } from './productionComposeModel';

export function NativeGraphicOverlay({ placement, frame, width, height, previewHeight, selected, onSelect }: {
  placement: ProjectedPlacement; frame: number; width: number; height: number; previewHeight: number;
  selected: boolean; onSelect: () => void;
}) {
  const effect = placement.graphic;
  if (!effect || placement.startFrame == null || placement.endFrame == null) return null;
  const local = frame - placement.startFrame;
  const span = placement.endFrame - placement.startFrame;
  const enter = effect.enter === 'none' ? 0 : effect.enter_frames ?? 6;
  const exit = effect.exit_frames ?? 6;
  const opacity = Math.max(0, Math.min(1, enter ? local / enter : 1, exit ? (span - local) / exit : 1));
  const scale = effect.enter === 'pop' && enter ? .85 + .15 * Math.min(1, local / enter) : 1;
  const defaultY = { question_card: .25, keyword: .5, title_bar: .12 }[effect.template];
  const size = effect.font_size ?? Math.round(height * { question_card: .06, keyword: .075, title_bar: .065 }[effect.template]);
  const style: CSSProperties = {
    position: 'absolute', zIndex: 40, left: `${(effect.x ?? width / 2) / width * 100}%`,
    top: `${(effect.y ?? height * defaultY) / height * 100}%`,
    transform: `translate(-50%, -50%) scale(${scale})`, opacity,
    color: effect.template === 'keyword' ? effect.accent ?? '#00BDCF' : effect.foreground ?? '#FFFFFF',
    backgroundColor: effect.template === 'keyword' ? 'transparent' : effect.background ?? '#111827',
    fontSize: `${size * previewHeight / height}px`, fontWeight: 700,
    padding: '0.18em 0.38em', maxWidth: '90%', textAlign: 'center', lineHeight: 1.25,
    whiteSpace: 'pre-wrap', cursor: 'pointer',
  };
  const emphasis = effect.emphasis && effect.text.includes(effect.emphasis) ? effect.emphasis : null;
  const parts = emphasis ? effect.text.split(emphasis) : null;
  return <div className={selected ? 'outline outline-2 outline-primary' : ''} style={style} onPointerDown={(event) => { event.stopPropagation(); onSelect(); }}>
    {parts ? <>{parts[0]}<strong style={{ color: effect.accent ?? '#00BDCF' }}>{emphasis}</strong>{parts.slice(1).join(emphasis ?? '')}</> : effect.text}
  </div>;
}
