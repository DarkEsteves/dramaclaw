import type { RemixArtifactRef } from '@/api/remix';
import type { NativeGraphicEffect } from './nativeGraphicEffects';
export interface PlanWordV2 { id: string; text: string; start_frame: number; end_frame: number }
export interface PlanSourceWordV2 { id: string; text: string; start_us: number; end_us: number; start_char?: number | null; end_char?: number | null }
export interface PlanSpeechTimingV2 { placement_id: string; media_hash: string; text: string; text_hash: string; source_in_us?: number | null; source_out_us?: number | null; evidence_ref: { artifact_id: string; revision: number }; words: PlanSourceWordV2[] }
export interface PlanTurnV2 { id: string; speaker: string; text: string; start_frame?: number | null; end_frame?: number | null; words?: PlanWordV2[]; speech_placement_id?: string | null; speech_timing?: PlanSpeechTimingV2 | null; timing_basis?: 'estimated' | 'manual' | 'aligned' }
export interface PlanBeatV2 { id: string; purpose: string; duration_frames: number; timing_mode?: 'auto' | 'fixed'; speech_pace?: 'slow' | 'normal' | 'fast' | null; extra_pause_seconds?: number | null; duration_source_placement_id?: string | null; turns: PlanTurnV2[] }
export interface PlanResourceV2 { id: string; node_id: string; node_revision?: number | null; media_kind: 'image' | 'video' | 'audio'; media_path: string; media_hash: string; duration_us: number; has_audio: boolean }
export interface PlanSpeechTakeV2 { id: string; beat_id: string; resource_id: string; turn_ids: string[]; origin_placement_id: string; source_in_us: number; source_out_us: number | null; speed_num: number; speed_den: number; lead_in_frames: number; volume?: number }
export interface PlanSpeechCueV2 { id: string; turn_id: string; take_id: string; text: string; source_start_us: number | null; source_end_us: number | null; timing_source: 'asr' | 'srt_import' | 'manual'; timing_status: 'draft' | 'ready' | 'stale' | 'review'; word_ids: string[]; evidence_ref: { artifact_id: string; revision: number } | null }
export interface PlanAnchorV2 { kind: 'program_frame' | 'beat' | 'turn' | 'word' | 'selection' | 'placement' | 'take' | 'cue' | 'text'; frame?: number | null; ref_id?: string | null; edge?: 'start' | 'end' | null; offset_frames?: number; char_offset?: number | null; text_hash?: string | null; affinity?: 'before' | 'after' | null }
export interface PlanSelectionV2 { id: string; label: string; start: PlanAnchorV2; end: PlanAnchorV2 }
export interface PlanPlacementV2 {
 id: string; kind: 'visual' | 'audio' | 'subtitle' | 'graphic'; role: 'primary' | 'overlay' | 'speech' | 'narration' | 'bgm' | 'sfx' | 'dialogue' | 'effect'; track_id: string; description?: string;
 speech_take_id?: string | null; resource_id?: string | null; source_audio?: boolean | null; turn_id?: string | null; start: PlanAnchorV2; end: PlanAnchorV2;
 source_in_us?: number; source_out_us?: number | null; speed_num?: number; speed_den?: number; loop?: boolean; z_order?: number;
 x?: number; y?: number; width?: number | null; height?: number | null; fit?: 'stretch' | 'cover' | 'contain'; opacity?: number; volume?: number; text?: string | null;
 subtitle_x?: number | null; subtitle_y?: number | null; font_size?: number | null;
 graphic?: NativeGraphicEffect | null;
}
export interface ProductionPlanV2 {
 authoring_stage?: 'outline' | 'arranged' | null;
 schema_version: 2; goal: string; output: { width: number; height: number; fps_num: number; fps_den: number; background?: string };
 speech_takes?: PlanSpeechTakeV2[]; speech_cues?: PlanSpeechCueV2[];
 beats: PlanBeatV2[]; selections?: PlanSelectionV2[]; resources: PlanResourceV2[]; placements: PlanPlacementV2[];
 provenance: { blueprint_ref?: { artifact_id: string; revision: number } | null; source_plan_ref?: { artifact_id: string; revision: number } | null; branch_source_ref?: { artifact_id: string; revision: number } | null; author?: string | null };
}
export type ProductionPlanV2Ref = RemixArtifactRef;

export function emptyProductionPlanV2(goal = '', blueprintRef?: RemixArtifactRef | null): ProductionPlanV2 {
 return { schema_version: 2, goal, output: { width: 1080, height: 1920, fps_num: 30, fps_den: 1 },
  beats: [], resources: [], placements: [], provenance: blueprintRef
   ? { blueprint_ref: { artifact_id: blueprintRef.artifactId, revision: blueprintRef.revision } } : {} };
}
