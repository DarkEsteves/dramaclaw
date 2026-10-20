import { describe, expect, it } from 'vitest';
import type { ProductionPlanV2 } from './productionPlanV2Model';
import { orderPlanSpeechTakes } from './productionSpeechTakeOrder';
const plan = (): ProductionPlanV2 => ({ schema_version: 2, goal: 'order', output: { width: 1280, height: 720, fps_num: 30, fps_den: 1 }, beats: [{id: 'beat', purpose: 'talk', duration_frames: 90, turns: [{id:'a',speaker:'A',text:'a'}, {id:'b',speaker:'B',text:'b'}, {id:'c',speaker:'A',text:'c'}]}], resources: [], placements: [], provenance: {}, speech_takes: [] });
const take = (id: string, turn_ids: string[]) => ({ id, beat_id: 'beat', resource_id: 'media', turn_ids, origin_placement_id: id, source_in_us: 0, source_out_us: null, speed_num: 1, speed_den: 1, lead_in_frames: 0 });
describe('speech take playback order', () => {
 it('orders takes and their grouped turns by author dialogue order', () => { const draft = plan(); draft.speech_takes = [take('late', ['c','b']),take('early',['a'])]; orderPlanSpeechTakes(draft); expect(draft.speech_takes?.map(t => t.id)).toEqual(['early','late']); expect(draft.speech_takes?.[1].turn_ids).toEqual(['b','c']); });
 it('rejects interleaved instances without changing their arrays', () => { const draft = plan(); draft.speech_takes = [take('shared',['a','c']),take('other',['b'])]; const before=JSON.stringify(draft); expect(()=>orderPlanSpeechTakes(draft)).toThrow(/interleav/); expect(JSON.stringify(draft)).toBe(before); });
});

import { acceptPlanSpeechTimings, bindPlanSpeechSource } from './productionPlanV2Edits';
function sourcePlan() { const draft = plan(); draft.resources = [{ id:'media',node_id:'node',media_kind:'audio',media_path:'voice.wav',media_hash:'a'.repeat(64),duration_us:1_000_000,has_audio:true }]; draft.placements = ['old','new','latest'].map(id=>({id,kind:'audio',role:'speech',track_id:id,resource_id:'media',start:{kind:'beat',ref_id:'beat',edge:'start'},end:{kind:'beat',ref_id:'beat',edge:'end'}})); return draft; }
const timing = (placement_id: string,text: string) => ({placement_id,media_hash:'a'.repeat(64),text,text_hash:'b'.repeat(64),evidence_ref:{artifact_id:'words',revision:1},source_in_us:0,source_out_us:1_000_000,words:[{id:`${text}-word`,text,start_us:0,end_us:500_000}]});

describe('editing preserves dialogue playback order',()=>{
 it('inserts an earlier rebound turn before a later existing take',()=>{const draft=sourcePlan(); bindPlanSpeechSource(draft,'a','old'); bindPlanSpeechSource(draft,'b','new'); bindPlanSpeechSource(draft,'a','latest'); expect(draft.speech_takes?.map(t=>t.turn_ids)).toEqual([['a'],['b']]);});
 it('orders ASR acceptance independently of completion order',()=>{const draft=sourcePlan(); acceptPlanSpeechTimings(draft,{b:timing('new','b')}); acceptPlanSpeechTimings(draft,{a:timing('old','a')}); expect(draft.speech_takes?.map(t=>t.turn_ids)).toEqual([['a'],['b']]);});
 it('rejects interleaved rebinding without partially changing author state',()=>{const draft=sourcePlan(); bindPlanSpeechSource(draft,'a','old'); bindPlanSpeechSource(draft,'b','new'); bindPlanSpeechSource(draft,'c','latest'); const before=JSON.stringify(draft); expect(()=>bindPlanSpeechSource(draft,'c','old')).toThrow(/interleav/); expect(JSON.stringify(draft)).toBe(before);});
});
