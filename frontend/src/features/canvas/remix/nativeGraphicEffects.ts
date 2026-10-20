export type GraphicTemplate = 'question_card' | 'keyword' | 'title_bar';
export type GraphicEntrance = 'none' | 'fade' | 'pop';

export interface NativeGraphicEffect {
  template: GraphicTemplate;
  text: string;
  emphasis?: string | null;
  foreground?: string;
  background?: string;
  accent?: string;
  enter?: GraphicEntrance;
  enter_frames?: number;
  exit_frames?: number;
  x?: number | null;
  y?: number | null;
  font_size?: number | null;
}

export const GRAPHIC_TEMPLATES: Array<{ id: GraphicTemplate; label: string }> = [
  { id: 'question_card', label: '问题卡片' },
  { id: 'keyword', label: '关键词强调' },
  { id: 'title_bar', label: '标题条' },
];

export const graphicTemplateLabel = (id: GraphicTemplate) => GRAPHIC_TEMPLATES.find((item) => item.id === id)?.label ?? '图形效果';

export function defaultGraphicEffect(): NativeGraphicEffect {
  return { template: 'question_card', text: '问题卡片', foreground: '#FFFFFF', background: '#111827',
    accent: '#00BDCF', enter: 'fade', enter_frames: 6, exit_frames: 6 };
}
