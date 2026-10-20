# Reference video recreation on the canvas

Do not automatically upgrade historical parameter choices into a new confirmation.

## Current-stage decisions

First identify what the user wants to 保留, 替换, 新增 or leave undecided. Keep
each confirmed constraint attached to the relevant plan entity. A changed
creative direction uses the saved analysis and current commission; it does not
start another analysis. An ordinary user reply may approve, amend, question,
pause or reject the proposal. Interpret the whole reply in its saved proposal
context, and ask when unclear. Do not treat a keyword or quoted approval as
authorization. The canvas action still follows its native confirmation mode.

For visual preservation, call `freezone_list_reference_media` with the actual
saved analysis node. Choose a real source clip, imported frame or accepted
asset for every preserve constraint and verify its media kind and scope. Pure
text generation may produce new elements, but cannot prove an unchanged
person, product or scene. A source video is not an image reference. If the
current model or native import action cannot realize the required preservation,
explain the specific gap and ask which constraint may change. A successful
generation is only a candidate until native content review accepts it.
When the user keeps a visual identity, start from traceable source frames or
existing project media and preserve their versioned provenance. Generate new
visuals for elements the user wants to replace or create; choose this from the
current request rather than from a fixed character, product or shot template.

## 原片证据与关键帧导入

`freezone_list_reference_media` 是原片证据和素材引用的只读清单。视频证据可以用于结构、动作、节奏、台词和环境声判断；视频 URL 不能直接填入图片参考。图片参考必须来自清单中已验证的图片项，并优先使用它的 `existing_node_id`。

原片分析事件/抽帧窗口只表示原片的观察证据，用于理解叙事、动作、反应、镜头变化和节奏，不等于复刻视频必须生成同样数量的镜头、beat、素材或单镜时长。事件数量可以作为导演编排的参考；镜头数量和编排由 Agent 根据新目标、台词、动作、反应、节奏和可用素材判断，可以采用相近数量，也可以合并、拆分、跳过或重组。不要把分析事件数量自动转换为镜头数量或固定时长。向用户复述分析时使用“原片分析事件/证据窗口”；如果提出复刻镜头数量，要说明那是新目标下的导演编排决定。1.5–2.5 秒只能作为原片节奏观察，是否沿用由 Agent 判断；生成素材仍按 4–15 秒及模型能力规划。

当用户要求保留原片视觉形象，而图片项只有 `shot_number`、没有
`existing_node_id` 时，查看画布动作目录，在分析节点上调用
`import_reference_keyframes`，需要时传入相关 `shot_number`，再重新读取
`freezone_list_reference_media`，只绑定动作返回和清单确认过的当前图片节点。这个动作只把已分析关键帧落到画布，不生成媒体、不确认素材选择。

如果没有可验证关键帧，或用户明确选择风格重建，才使用生成参考图路径，并说明它是新的生成候选。导入失败时只报告失败原因和下一步建议，不自动生成、不自动重试。不要把生成图说成保留原片形象，也不要仅因存在原视频就导入全部关键帧。保留原有画布和动作确认流程，绑定和付费生成仍等待用户确认。

Use `freezone_retry_material_step` only to rerun the same saved strategy.
When the user changes source, reference or generation method, read the latest
work revision and use `freezone_revise_material_strategy` with complete
replacement steps in one related batch. Preserve independent accepted media;
after a revision conflict, reread and merge only the still intended changes.
Submit asynchronous native work and end the turn. The host sends a completion
event with its saved result; 不要循环轮询 task or node status. A single status read
to choose the next action is allowed. On repeated failure, change the actual
invalid parameter or strategy rather than resend the same request.

Use this guide with the catalog Skill `reference-video-remix`. For dynamic media planning, confirm its enabled
state, Recipe list, input contract and registered actions from the authenticated
host. The catalog ID is stable. Follow the returned contract and preserve a
user's saved execution choices; do not assume a local JSON file overrides them.

## Production shape

The canvas route is reference video → reference analysis → one or more independent
production plan nodes → one video composition node for each plan. Image, video
and audio asset nodes feed the relevant plans. Speech correction, subtitles,
semantic placements and graphics live inside the production plan. Do not create
an intermediate semantic timeline, script or SRT canvas node for this route.
Each plan has its own artifact ID and revision; plans may share immutable
analysis evidence and versioned media.

Use only the actions and Recipes returned by the host. The
`reference-blueprint` Recipe extracts transferable narrative structure from
actual reference evidence. The `reference-production-plan` Recipe returns the native editable plan
shape directly. Validate and save that single plan through the authorized
production plan service. A textAnnotationNode containing JSON does
not create an editable plan. The old `reference-remix-script` and
`reference-broll-plan` artifacts remain readable for previous jobs; the new
route does not require them. Media Recipes are selected only for actual missing
assets.

## New-plan tool contract

Use the current commission, analysis binding and `native_plan_stage` returned
by the host. A Skill package lookup is optional guidance discovery, not a
prerequisite for the native production entry. Run `run_video_analyze_story` on the source video
only for an explicit manual standalone analysis. For a recreation workflow that
has no completed analysis, call `freezone_start_reference_analysis` with the
actual source node ID, user goal, current inputs and a stable idempotency key;
end the turn after submission. Do not poll nodes or tasks. The server stores
the task/run identity, saves actual results and sends a system directive to the Agent before
resuming a fresh turn. On that continuation, use the supplied real analysis ID.
For follow-up changes to product, dialogue or style on an existing reference,
read `freezone_get_production_progress` and reuse the matching saved analysis
and commission. If `freezone_start_reference_analysis` returns
`reference_analysis_reused`, continue this same turn; no analysis was submitted.
Read `freezone_get_reference_director_state` and present the real analysis.
Discuss the direction in ordinary chat as a short creative brief: what to keep,
what to replace, and explicit user constraints. Questions, comparisons and
revisions remain conversation; do not save a direction proposal on each reply.
Do not invent a shot-by-shot script, complete dialogue or asset list here; the
plan node's AI draft owns those details. When the user asks to proceed, gather
one complete but concise goal and call `freezone_prepare_production_nodes` with
the current `commission_ref` and `native_plan_stage`. The host checks the
authenticated human-turn binding, current canvas and commission revisions, and
the typed `requirements`/`preserve_replace` fields before saving the direction;
it does not make a second semantic model call. Replies resolving the creative
choices you just asked about can request the next planning step in context; no
special confirmation word is required. Respect explicit instructions to discuss
only, wait, or create without drafting. Do not infer approval from a model
assertion or display a direction card. Never call
`freezone_prepare_reference_decision` for direction, including after a
deterministic submission rejection.

On `direction_submission_invalid` or `direction_source_changed`, read the
returned field path and current references. Fix only the malformed typed input
or reread the current saved revision; ask the user when the creative content is
actually unclear. Do not repeat an unchanged rejected request, create a
direction decision, or claim nodes or drafting were submitted. There is no
automatic retry.
If the stage is `unprepared`, call the native entry once with this request:

```json
{
  "commission_ref": {"artifact_id": "COPY_ACTUAL_COMMISSION_ID", "revision": 1},
  "goal": "The complete creative goal and changes confirmed by the user",
  "run_after_create": true,
  "plan_count": 1
}
```

Copy the actual saved reference including its current revision; the example's
revision is illustrative. Understand the user's reply in context and preserve
all confirmed requirements in a concise `goal`, without adding invented shot
timings or dialogue. The plan task generates the complete editable production plan in one model call: structure, dialogue, resources and visual/audio arrangement (`authoring_stage=arranged`). After completion, read the saved result, check the user's goal and perform the normal full plan review with bounded edits. Only change `plan_count` when the user requests multiple plans. Keep the Agent's conversation at the direction level; the node AI supplies the detailed plan. An accepted task is not a saved plan. Here `run_after_create=true` means creation plus
`generate_production_plan_draft`: AI drafts an editable production plan after the
existing canvas confirmation. It does not authorize image/video/audio material
generation or final rendering. Use `false` when the user only requests node
creation or explicitly excludes drafting; retain that scope during recovery. The host resolves the bound completed analysis and confirmed scope.
Do not hand-author generic Intent, external input bindings, or a
`workflow_result` operation for this entry. Catalog discovery is not a
prerequisite for native plan creation.

The tool prepares the existing workflow draft and submits its exact revision
through the existing canvas approval. In manual mode wait for that command
card: approval must precede node creation and generation. Do not display another
direction or workflow card or separately confirm the prepared draft. The
approved compiler creates native plan nodes and, when the confirmed scope
requires them, linked composition nodes. Creation-only returns creation
receipts and does not submit generation. When creation and generation were
authorized, this same approval submits the existing plan drafting action;
reuse its task receipt and end the turn at `await_completion_event`.

For an existing bound plan node, inspect its actual saved state. Audit/edit an
existing saved plan; only an empty `ready_to_submit` slot and user authorization
allow `freezone_run_node_action` with the current catalog's registered
`generate_production_plan_draft` action and that exact `plan_node_id`. Manual
mode uses the action's existing approval. A queued/running task belongs to the
host: end this turn at `await_completion_event`, without polling or resubmission.
The host resumes after a real completion/failure event. Read the saved
`plan_ref` before auditing or editing. A created node is not a generated plan;
a task acceptance is not completion, and `phase=drafting_plan` alone is not
running evidence. Report only the actual approval, creation, task or saved-plan
receipt. When `native_plan_stage.state=failed`, calling
`freezone_prepare_production_nodes` only reuses the existing draft and does not
submit a new task; do not report it as a retry. After explicit user
authorization, use the failed slot's exact `plan_node_id` with
`freezone_run_node_action(action=generate_production_plan_draft)` and retain
the existing canvas action confirmation. Only a fresh run receipt proves
resubmission; the retry action reuses the node's saved topic, requirements and
blueprint, so do not pass an empty or newly invented payload. If those saved
parameters cannot be read, stop and report the blocker. Never retry
automatically.

On input errors correct only the returned fields; on stale references read the
current saved reference. On cancellation stop. On uncertain transport inspect
the existing draft/task receipt before any repeat submission. A failed actual
generation keeps its node and task: explain that failure and wait for user
authorization to retry. Do not automatically switch to generic planning,
reanalyze, create replacement nodes or make another paid generation attempt.
Missing assets and speech are resolved before composition in a later stage.
Media-only generation stages may still use explicit allowed Recipe
items with `include_compose=false`, then bind their real outputs to the plan.

For dialogue plans, anchor primary visuals in spoken beats to beat, turn or
accepted speech positions. Absolute `program_frame` ranges copied from the
reference are estimates and cannot pass semantic-anchor review. Create real
subtitle or graphic placements when the planned picture needs on-screen text;
descriptions alone do not render it. Keep speech timing pending until actual
audio is recognized, corrected against the intended lines and accepted. A
silent plan may use ordinary beat or program timing.

The draft task and current production state include `speech_estimates` and
`plan_diagnostics` for the exact `plan_ref`. These are local estimates, not
actual SRT or proof that the model performed the words. The AI chooses the
dialogue, pace, action and extra pause; the host may fill only a new auto spoken
beat's omitted `duration_frames`. Read the warnings, inspect the actual beat,
turn and placement positions, and use the existing production update command
to correct content or timing. A new revision requires a new review. Do not
overwrite saved fixed timing or user edits with an estimate.
The estimate records distinguish an authored pace or pause from a default,
and a newly initialized spoken duration from an authored or saved duration.
Changing `speech_pace` or `extra_pause_seconds` changes the estimate only;
change an already saved `duration_frames` explicitly through the normal canvas
approval when the intended beat length must change. Inspect whether each kept
reference event, action, reaction, cut and meaningful pause is present in the
saved plan; a matching total estimate alone is not a complete review.

Plan sound from its actual job and source, in this order: decide whether each
moment needs performed dialogue, narration, music, an effect, source video
sound, or silence; identify which visual or audio placement actually supplies
that sound; check the selected model's sound capability and the user's already
confirmed parameters; reuse suitable accepted project media; prepare only the
missing source; review what the resulting audio actually contains; then adopt
measured word timing when speech needs alignment. A B-roll picture does not
create narration. The number of lines or speakers does not determine the
number of audio files: contiguous lines in one speaking clip share one source
and one playback take. Use `source_audio=true` on an ordinary video placement
only when its original action or ambient sound is intended to play; a speaking
video's shared take already owns its voice. Keep an unbound planned source
explicit until real media is attached; do not run ASR on a placeholder or infer
correct speech from `has_audio=true`. Reuse a voice sample only when stable
voice identity is needed and the chosen model supports it. Do not synthesize
TTS for each line before checking whether the video itself performs that line.

Before preparing a material work package, read the current direction's
`preserve_replace` choices and call `freezone_list_reference_media` for the
saved analysis node. A visual-identity preservation choice needs a real image
source and a `preservation_bindings` path to each affected final material.
Keeping only narrative structure does not require preserving the original
person's image. Use existing frames or an image reference where appropriate;
do not silently erase a binding to make generation pass.

For each generated video step, write its own integer `durationSec` in the
4–15 second range and check the selected model's live duration, resolution,
mode and sound capabilities. A short final shot may share a longer generated
clip or select a usable interval; a long performed action may need several
clips. Do not copy each beat's length into the generation request, and do not
ask the user for one global generation duration. Read the latest saved plan's
dialogue, action descriptions, speech ownership and estimated timing. Count a
spoken turn once when several visuals or subtitles refer to it, and each beat's
explicit pause once when combining adjacent dialogue. Group a brief answer
with its reaction or neighboring question into a useful generated performance;
split long performances at a natural action or conversational boundary. A
1–2 second final cut can use part of a longer generated asset. The estimate
is not actual speech alignment. The user's submitted model,
ratio and resolution choice is required for each generated media type. A
recommendation or workflow approval marker is not that choice. Use the
existing parameter clarification and native canvas/action confirmation;
do not start paid generation on a missing choice. Read the actual Skill package
to obtain `reference-video-remix`'s current version before registration.
After a candidate is accepted, align its actual speech once for the shared
source; use measured words for auto subtitles and semantic positions. Silent
clips have no speech alignment task.
Edit source ownership and bind media through the native production command
contract, using the current plan revision and preserving existing review and
timing evidence rules. An old independent audio placement is never silently
merged into a video source by its label or similar description; review and
remove any redundant placement explicitly after changing the source.

## Progression

Start and resume with `freezone_get_production_progress(commission_id?)`.
Without a commission ID it returns the actual independent `branches`; choose
the user's intended commission before acting. Its current `plan_ref` and
`work_ref` define this production, even when newer historical work exists.
For plan creation, use that branch's `native_plan_stage`: `unprepared` means
prepare one draft, `awaiting_confirmation` means wait for canvas approval,
`ready_to_submit` names the bound node to draft, `running` means wait for the
host event, and `ready_for_review` means read the saved plan. Other branches
and a newly selected canvas node cannot replace this binding.
If `next_action.kind=select_material_work`, inspect `candidate_work_refs` and
use `freezone_select_material_work` only for the user's explicit work choice,
with current commission/work revisions and a stable command_id. A sole
`suggested_work_ref` is a suggestion, not an adoption receipt.

Use the saved `progress`, `execution_status`, `blockers`, `problem` and
`execution_problem`; do not sum historical reviews or visit every node to
calculate progress. For `next_action.owner=system`, call
`freezone_advance_production(commission_ref, expected_projection_revision,
command_id)`, copying `projection_revision` verbatim into
`expected_projection_revision`. This performs one deterministic eligible action
using current host evidence. Task identity, result registration, saved passed
review adoption, final media binding and compilation are owned by the host;
only the host supplies `ready_system_actions` and executable action IDs.
The tool cannot confirm user decisions or start a new paid generation wave.
Agent actions still require real creative authoring or registered native tasks.

Read already saved direction and generation parameters before asking again.
Keep confirmed model, duration, resolution, reference roles, budget and scope;
a genuinely new parameter or substantive proposal change follows its existing
confirmation. Running/recovering means end the turn and wait for actual host
completion events. Waiting for user review is normal. Stopped means preserve
results and await explicit resume. After a version conflict, reread this same
commission before editing or retrying; never select another work to evade CAS.

The video-generation card's duration is the length of **one generated source
clip**. It is not the final composition length or a per-beat limit. Derive the
plan's beats and total program duration from the analyzed reference and the
confirmed preserve/replace direction unless the user explicitly requests a
different final length. For example, a 4-second generated clip setting does
not compress an approximately 18-second reference into a 4-second plan. Keep
the source's dialogue order, pauses and scene relationships; use accepted SRT
anchors when arranging voiced beats.
Creating a production plan or composition node is not a media-generation step:
do not ask for image/video duration merely to submit its draft. At material
generation, read the current plan placements and accepted speech timing. Give
each video step its own durationSec supported by its selected live model; a
shorter edit may use a reviewed selection from a longer generated clip. If a
continuous shot exceeds model limits, redesign its visual coverage or discuss
the constraint with the user. Never truncate confirmed speech, silently extend
the program, or apply one source-clip duration to every step. Reuse confirmed
model, ratio, quality and audio preferences when they remain applicable.
When the user keeps the reference's structure, account for each analyzed
event in a saved beat or explicit placement. A long beat labeled “fast-cut
montage” needs the actual internal shots and speaker reactions as placements;
it cannot silently absorb distinct setup, rebuttal and payoff events. In plan
review, mark the narrative or direction finding `change` when that mapping is
missing, even if the total duration and schema are valid.

Recovery uses host `code`, `retryable`, `retry_scope`, `next_action`,
`required_user_action` and `existing_result_ref` when returned. In particular,
`result_registration_pending` retains the completed result and uses the real
system settlement action. Waiting, content-unreviewable, provider-failure and
version-conflict responses have distinct repair scopes. A tool failure does
not authorize regeneration. Preserve actual error/blocker status and actual
refs; no example ID is executable evidence.

1. Resolve the reference source and user goal from the selected node, uploaded
   media or authorized project assets. Record explicit preserve/replace rules,
   output requirements and requested deliverable scope. Read the existing
   analysis and plan revisions before requesting more work.
2. Reuse the current `videoStoryNode` analysis for the same source revision.
   When absent in a recreation request, use `freezone_start_reference_analysis`
   and end the turn. The server resumes after the joint evidence, transcript (when needed) and
   `VideoStory` result is saved, to discuss the recreation direction.
   The manual `run_video_analyze_story` action remains available for standalone
   analysis. Read the completed result's `rawResult` and saved
   `referenceBlueprintRef` where present. The lower-level
   `analyze_reference_video` task produces cuts and keyframes in
   `reference_evidence.v1`; those alone do not establish narrative meaning.
   Separate observations from inferences and user choices; do not derive
   reference content from a filename.
3. Create only the plans requested in the current bound production branch. A
   native `scriptNode` carries `remixRole=production_plan`; each plan has its
   own artifact and linked `videoComposeNode`. When the current slot says
   `ready_to_submit`, use its `plan_node_id` with the registered
   `generate_production_plan_draft` action and a real creative `topic`. The
   host supplies the saved `blueprint_ref` or bounded `analysis_context` and
   records the exact task and result. Submission ends this Agent turn; the
   host publishes the validated result to that same node and resumes the Agent.
   Read the saved plan and exact ref before review or later stages. Do not
   treat a submitted task as a saved plan, attach refs yourself, or submit a
   later stage using an unresolved plan ref. Technical generation choices
   such as `video_duration_seconds` remain structured inputs when an actual
   video-generation node needs them. This duration governs one source clip,
   not the full program length or `topic` text.
4. Inspect the plan's missing assets. Reuse accessible media where it satisfies
   the role; otherwise use an allowed image, video or audio Recipe and the
   registered generator. Bind successful media by project-scoped resource ID
   and version. A frame and its soundtrack have separate roles: showing a
   speaking video does not authorize duplicate audio playback.
   Read the native `production_plan.plan_ref` from node context or
   `freezone_get_node_detail`; copy its `artifact_id` and `revision` into
   `freezone_get_production_state`, never the canvas `node_id`. A generic empty
   script prompt does not mean the native author document is absent. Respect
   the context's `dirty` and `conflict` flags before editing saved state.
   Review actual saved placement descriptions and graphic text/emphasis, and
   numeric `duration_frames`/`timing_mode` with output fps. Goal or beat purpose
   prose cannot replace actual shots/effects or establish the real duration.
   The compact summary is insufficient for authoring: read the saved placement
   with `freezone_get_production_state(entity_id=...)` before `upsert_placement`.
   Preserve all untargeted fields and anchors from that entity. Any truncated
   preview requires an entity read before review or editing.
   Read entities with `freezone_get_production_state`, and submit bounded edits
   with `freezone_prepare_production_edit`. `bind_media` targets a placement and
   uses an existing resource ID or a validated current canvas resource. While
   a speaking video is only planned and has no `resource_id`, attach the intended
   source through `edit_turn.speech_placement_id`; do not call
   `bind_speech_source` yet. That command requires an already bound, audible
   project video or audio resource. Once media is bound, target the beat ID
   with its audible `placement_id` and an explicit ordered, contiguous
   `turn_ids` group. Read the saved take ID before calibration; do not treat two speakers
   in one clip as two separate playback instances.
   If an existing shared take has only draft timing, use
   `reassign_speech_source` to move its complete ordered turn group to a new
   audible placement or explicit null source in one native edit. A bound new
   source creates a new draft take and cues. Accepted timing, word anchors,
   and dependent take or cue anchors block this reassignment; resolve that
   evidence explicitly before changing the source.
5. For dialogue with a reusable candidate observation, follow the candidate
   review/adoption route below. Otherwise save the current plan revision before running
   `align_production_speech` with its exact `plan_ref`, `media_path`, `turn_id`
   and `take_id` or `placement_id`. Recognize the **complete speaking source** once per source
   revision, including a single clip with multiple speakers. Match turns in
   declared order and save evidence before timing placements. Recognized words
   are observations; the production plan text is the intended dialogue. If
   words differ, show the difference and request or use an explicit correction.
   Never replace recognized words to claim acoustic agreement. Sentence SRT
   supports sentence placement; a word-level effect requires real word timing
   and stable word identities. Preserve source-time pauses even when the
   estimated beat duration is shorter. For sections without dialogue, use
   visual, media-duration or music rhythm; do not require ASR or invent SRT.
   On an existing plan use `run_node_action` with `align_production_speech`,
   `parameters.turn_id` and one of `parameters.take_id` or
   `parameters.placement_id`. The host resolves the saved plan revision and
   complete bound media. The result may be blocked for review while still
   preserving its recognition artifact; inspect and correct it before retrying.
6. Express shots, overlays, graphics, subtitles and sound against stable
   plan entities and valid sentence/word/range anchors. Distinguish source
   media time, program time and effect-local time. Read the latest saved
   `plan_ref` after media binding and speech acceptance. Use the current system
   next action through `freezone_advance_production` for deterministic compilation.
   For an explicit manual compose operation, submit
   `compile_production_plan` on the linked native compose node with that exact
   ref; wait for its validated `composition_manifest.v2` result and attach it
   only to the owning compose node. If a later action lacks a saved ref, report
   a resumable blocker and continue the ready stage. The Agent may use several
   approved workflow runs to complete this sequence. Preview and render only
   from the same valid revision.
   Use the compose node's `compile_production_plan` action, then
   `auto_compose_video` for the requested export. A completed old export is not
   current after the saved plan or connected materials change.
7. Review actual media when available. Structural checks alone cannot certify
   picture and sound quality. On edits, reread the current revision, update
   only affected entities, and rerun only invalidated operations. A stale job
   result may remain as a candidate but must not overwrite a newer human edit.
   Replacing accepted dialogue or speaking media requires an explicit
   `invalidate_speech_timing: true` on the bounded edit. The cue identities and
   old evidence remain traceable, and the changed source requires calibration.
   If existing word anchors cannot survive the change, show the affected
   relationships and arrange an explicit remap; never broaden them to a whole
   sentence silently.

## Saved evidence and runtime outcomes

On a completion or reconnect, first read the run and
`freezone_get_reference_director_state`, then the current saved analysis or
plan with `read_consistency="saved"`. A host snapshot or protected artifact
read can supply the real summary while the page is closed. Identify the read's
`source`, revision and coverage; `last_successful_context_receipt` belongs to
its original turn and source version. A saved read is evidence, not a canvas
write receipt. Use bounded pagination only for missing content, not repeated
reads of the same unchanged node. Present the observed summary before asking
for a missing direction decision.

Choose the next action from the actual result:

| Observable result | Next action |
| --- | --- |
| `requires_browser`, `no_connection`, or `execution_blocker.code=browser_required` | Keep saved analysis and the original request; ask to open the matching canvas only for live selection, unsaved author state or browser execution. Resume the owned request after reconnect. |
| `canvas_revision_conflict`, production `conflict` or `dirty` author state | Reread the current saved revision and let the user preserve/resolve pending edits; apply only an authorized delta with current CAS. Never replace newer user content. |
| `execution_blocker.code=user_required` or a pending native decision | Explain direction and reviewed plan proposals in ordinary chat and wait for the user's reply. For native material approval, recover its existing card. Retain the saved decision identity and do not ask the same question again. |
| A submitted task or saved running production stage | End the turn and wait for the host completion event. Do not poll the same node or resubmit generation. |
| A failed/expired tool channel with saved analysis intact | Report the actual blocker and retry the current read/delivery stage using its run and task identities. Do not request another video analysis. |

A normal model finish is not proof of a successful read or delivery. Honor the
saved `execution_blocker`, current commission phase and native receipts. A
successful protected read may resolve an earlier channel failure; a real user
decision remains a separate host event. Scope includes the authenticated actor,
project, private canvas, Agent and original turn. Reconnect history cannot grant
execution to a different scope or approve paid media.

## Protected operations and recovery

Use the authenticated project's registered remix operations, production
commands and workflow approval surface. The identity and project scope come
from the host, not from model-supplied paths or tenant IDs. Writes require an
expected revision and stable idempotency key. For a revision conflict, read the
new plan, show the changed entities and reapply an authorized delta. A task
that completed while the browser was closed is resumed from its stored
operation/result reference. Do not repeat paid generation solely because the
chat lost context.

Do not use arbitrary file writes, raw canvas JSON patches or repeated
single-node tools to bypass the protected workflow batch. If the host has not
registered the action needed to create or edit a typed plan, report the exact
missing operation and stop that stage. Continue useful read-only inspection
without claiming the video was recreated.

## Director stages and concrete tool contracts

The reference is user-supplied or explicitly selected project video. If it is
missing, ask the user to upload it to the canvas; do not fabricate analysis.
First read the whole argument and picture/sound relationships. Use
`freezone_get_reference_evidence(artifact_id, revision, start_us?, end_us?,
cursor?, limit?)` for bounded facts and real source frames. Audio presence does
not establish speech; no-speech work can finish analysis normally. Preserve
unknowns and the actual sampling coverage.

`freezone_get_reference_director_state` returns saved commissions, pending
plan decisions, material work and speech corrections. Discuss which narrative
and visual relationships to keep and which people, products, words and sound
to replace. Distinguish reusing a method from adopting original media. Retain
prior answers and ask only about missing decisions or inputs. The ordinary
conversation itself does not create a formal direction decision.
For a confirmed request to preserve the original visual identity, give the
preserve_replace choice a stable `id`, `entity_kind`, `entity_id`,
`action: preserve`, and `visual_identity: true`. Do not set that flag for
shared story structure, character relationship, pacing, or a newly designed
replacement. The downstream material work must trace each flagged choice to
real image bytes through `preservation_bindings`.

A human may approve, change, withdraw, or question the direction in any words.
Do not use keywords or a direction proposal card as the authorization test.
Only at the native preparation boundary does the host verify the current user
turn and the complete goal. A system continuation or Agent statement is not a
new human decision. A reviewed plan still uses its existing authenticated
confirmation flow; keep that separate from direction discussion.

When the user asks to proceed, follow the New-plan tool contract above:
use `freezone_prepare_production_nodes(commission_ref, goal, run_after_create,
plan_count, requirements?, preserve_replace?)` for an `unprepared` stage. The
tool opens existing canvas approval
and preserves the requested execution policy. For an existing empty bound
node, read its current `processingPayload` and node revision. If the user changes the goal before drafting, submit one complete
`processingPayload.topic` update through `freezone_update_node_data` and wait
for the existing canvas confirmation to apply it; the same node then owns the
new goal and revision. Do not prepare a second plan node. Use
`freezone_run_node_action` only after the updated node is current, its state
permits submission, and the user authorized generation. A running task keeps
its submitted input; later edits do not rerun it automatically. End the turn
on an accepted task and wait for its saved completion or failure event. Do not treat `next_action` as an
event or call the removed direct-plan creation tool.

Read the saved editable plan after the native draft action completes,
audit it against the confirmed direction and make bounded edits where needed.
Use `freezone_prepare_production_edit` for coordinated changes; in manual mode
wait for the user to approve each command card. Do not call the direct-plan
endpoint or create a plan or composition node outside the canvas approval flow.
Read the exact current plan reference before editing. Submit only real beats,
dialogue and placements that belong in the proposed plan; schema discovery or
a dummy “probe” beat would become an actual user approval request.

### Coordinated production edits

For coordinated content changes, propose one `batch` through
`freezone_prepare_production_edit` and its existing approval surface. Read the
exact current saved `plan_ref` first. The outer command has `command_id`,
`plan_ref`, `kind="batch"`, `target_id="plan"` and `payload.commands` (1–100
children). Each child has exactly `kind`, `target_id`, `payload`; it has no
`command_id` or `plan_ref`, and cannot itself be a batch. All children apply
atomically to one saved revision.

`edit_beat` accepts `purpose`, `duration_frames`, `timing_mode` (`auto` or
`fixed`). `edit_turn` accepts `speaker`, `text`, and explicit
`invalidate_speech_timing: true` only when an authorized text change invalidates
accepted timing. Existing word anchors still require the dedicated correction
flow. `insert_turn` requires `beat_id`, nested `turn: {id,speaker,text}` and
an explicit `index`; its `target_id` equals the new turn ID.

Do not send `direction` on `edit_beat`, `timing_status` on `edit_turn`, flat
`speaker`/`text` on `insert_turn`, or `beats`/`turns` on `batch`. Narrative
instructions must be translated into actual supported plan content; do not
stuff them into unrelated fields.

This example uses placeholder IDs and revision. Replace the plan reference
and existing IDs with the exact saved values, and choose a fresh turn ID:

```json
{
  "command_id": "edit-example-1",
  "plan_ref": {"artifact_id": "PLAN_ARTIFACT_ID", "revision": 1},
  "kind": "batch",
  "target_id": "plan",
  "payload": {"commands": [
    {"kind": "edit_beat", "target_id": "BEAT_ID", "payload": {"purpose": "Opening question", "duration_frames": 96, "timing_mode": "auto"}},
    {"kind": "edit_turn", "target_id": "TURN_ID", "payload": {"speaker": "Host", "text": "Why now?"}},
    {"kind": "insert_turn", "target_id": "NEW_TURN_ID", "payload": {"beat_id": "BEAT_ID", "turn": {"id": "NEW_TURN_ID", "speaker": "Guest", "text": "Here is why."}, "index": 1}}
  ]}
}
```

Call `freezone_review_production_plan` with the actual plan/commission refs and
entity-located reasons/evidence in all eight categories: dialogue, narrative,
audio_visual_roles, material_requirements, native_effects, output,
semantic_anchors, direction. Structure validity alone is not creative review.
First read the current production state's `review_context.requirements`, then
inspect the exact saved plan. For each semantic requirement copy its ID into
`requirement_id` and cite an actual saved `beat`, `turn`, `placement`, `resource`
or `selection` through `evidence_refs` (`entity_kind`, `entity_id`, `field`).
Use `field=purpose`, `text`, `description`, `media_path` or `graphic.text` as
appropriate; the server reads that field from the saved plan. If the plan goal
expands the confirmed direction, the direction finding also cites `plan.goal`
and one actual content entity. Explain your conclusion in `reason` and
`evidence`; copying the full field text into prose is unnecessary. Mark an
unmet requirement `change` or `unsupported` instead of inventing a citation.
Fix changes_required findings against the current saved plan and review again.
A passed review registers the exact pending plan decision internally. Explain
that reviewed plan in ordinary chat and ask the user to confirm, change, or
question it in their own words. Do not call `freezone_prepare_reference_decision`
for the same plan or make a separate confirmation card. For a clear approval of
that exact presented plan, call `freezone_confirm_reference_plan` with only the
saved `commission_ref` and `plan_ref`; the host binds it to the active human
turn and returns the confirmation receipt. Normal replies remain ordinary chat,
and neither the Agent nor a system notification can approve without that host
receipt. Work scope and material production begin only after the authenticated user confirms through the existing flow; the authenticated user confirmation is
the state transition, not an Agent assertion.

For several real saved plans, use `freezone_prepare_reference_branches` with
the confirmed parent commission and each exact plan_ref. Read each child
commission: it owns its own plan review, approval, material work and delivery.
A child can propose a different direction explicitly. The parent is complete
only after every current child has reached its requested delivery scope.

Plan how media is made before generating it. `freezone_prepare_material_work`
saves requirements (final placement/take/turn IDs and acceptance conditions)
and steps (source generate/existing/upload, media kind, Recipe, prompt,
dependencies and references with identity/product/setting/composition/motion/
voice/first_frame/last_frame roles). Use shared references where they carry
continuity. Do not copy dialogue into a separate mutable script.

When the confirmed direction explicitly preserves a visual identity, put its
stable choice ID, an actual current image node ID, and the reference role in
the final requirement's `preservation_bindings`. The final material must
reach that node through its direct references or referenced intermediate
steps. The host verifies the current media bytes and records their hash.
Preserving story structure or character relationships alone does not imply
preserving the original person's appearance. If the source is a video,
prepare a real image frame through the existing canvas action first; never
substitute its video URL for an image input or quietly use text alone.

Generation inputs follow the actual model catalog capabilities; a Recipe name does not
guarantee all reference modes are supported. A video can contain multiple
shots or speakers, and several placements can reuse one accepted source.

Material authoring uses native field names. `plan_ref` here and `work_ref` /
material `review_ref` in subsequent material operations contain exactly
`artifact_id` and integer `revision`; keep `project_id` / `canvas_id` at tool
scope. Keep `commission_ref` as returned. Read the current confirmed saved
plan first and replace with actual saved placement/take/turn IDs. A requirement
must include `id`, `description`, `media_kind`, `final_step_id`; its optional
`placement_ids`, `take_ids`, `turn_ids`, and `acceptance` describe final bindings.
A step must include `id`, `label`, `media_kind`, `source`; generate steps also
need matching `recipe_id` and nonblank `prompt`. `depends_on` contains step IDs;
`references` contains `{step_id,role}` or `{node_id,role}` with exactly one source.
Never use invented step fields `description`, `output_kind`, `placement_id`,
`step_kind`, or `uses`. A shared image step can feed multiple final video steps
without a final placement of its own. Execution and acceptance are server owned:
initial `status` is `planned` or `awaiting_upload`, never `accepted`/`submitted`;
do not author candidates, accepted resources, task IDs or execution receipts.
Optional `generation_inputs` carry supported model parameters only, never
identity, prompt, Recipe or `reference_inputs` overrides. Existing/upload steps
omit `recipe_id` (or use null); use real canvas nodes and await actual uploads.
For each generated video step, set `generation_inputs.durationSec` from the
placement's intended use and the selected model's current limits. A missing
production-step duration is a planning error naming that step, not a request
for a global duration card. Keep the edit interval in the plan placement; do
not force it to equal the length of a generated source clip.

Each final placement or speech take has one resource slot. Different
`final_step_id` values cannot claim the same `placement_ids` or `take_ids` target.
Several requirements may name the same final step; one accepted final source may
serve several distinct saved targets. `turn_ids` are context and may be shared.
Shared identity images stay intermediate references without final picture
bindings. A main shot and each B-roll need separate actual saved placements.
Read the current plan and use `freezone_prepare_production_edit` to add or correct
those placements/takes before preparing new work; never invent target IDs or
bind a reference image and a final video to one slot. Historical work remains
readable for correction, but a conflicting package cannot start its next stage
or pass final completion.

<!-- native-material-author-example -->
Bounded shape example for one shared image and one final clip. Replace the
example refs, final `picture` placement and prompt with the actual confirmed
plan, creative content and actual server-issued admission; this is not a command
to run unchanged. The execution option below applies when material production
has been requested.

```json
{
  "commission_ref": {"artifact_id": "commission", "revision": 1},
  "plan_ref": {"artifact_id": "plan", "revision": 1},
  "command_id": "material-graph-1",
  "operation_id": "COPY_ACTUAL_SERVER_RETURNED_OPERATION_ID",
  "run_after_create": true,
  "requirements": [
    {"id": "final-clip", "description": "Final product clip", "media_kind": "video",
     "final_step_id": "clip", "placement_ids": ["picture"], "acceptance": ["Product identity stays consistent"]}
  ],
  "steps": [
    {"id": "shared-image", "label": "Shared product reference", "media_kind": "image",
     "source": "generate", "recipe_id": "general-image", "prompt": "Confirmed product in the agreed setting"},
    {"id": "clip", "label": "Final product clip", "media_kind": "video",
     "source": "generate", "recipe_id": "general-video", "prompt": "Agreed action and camera motion",
     "depends_on": ["shared-image"], "references": [{"step_id": "shared-image", "role": "first_frame"}]}
  ]
}
```

The tool prepares only the ready wave through existing workflow approval.
When the user requests only some saved placements, pass their exact IDs as
`placement_ids` to `freezone_prepare_material_stage`; the host includes required
dependencies and reuses accepted steps. Inspect `generation_scope` before
submission. A waiting, unknown-admission, upload, or retry-required step is not
a fresh generation request. Let the existing workflow finish and notify this
conversation; do not poll or create another work package merely because a
candidate has not appeared yet.
For each ready wave, call `freezone_begin_agent_product_generation` with
`product_kind=workflow_result` and the current saved Skill identity. Read the
actual server admission receipt and copy its exact operation_id into
`freezone_prepare_material_work` or `freezone_prepare_material_stage`. Never invent
or reconstruct an admission ID, bypass a rejected admission, or reuse a terminal
operation. Use `run_after_create=true` when the user requested material production;
confirming that draft then starts the native generation workflow. If this is
only preparation, preserve the agreed execution choice. These options belong at
tool scope and pass into the existing workflow draft approval; they are not
material graph fields. Approval, model preflight and candidate review remain
required.

If material preparation returns `workflow_result_admission_required`, its work
is already saved. Reuse the exact returned `work_ref`; after a successful native
admission, copy its real returned `operation_id` at top level into
`freezone_prepare_material_stage(work_ref, operation_id, run_after_create=true)`
when actual material production was requested. Retain the agreed execution choice
for preparation only. Do not register again when a valid nonterminal admission
for this wave already exists, or create a duplicate work package. Do not switch
to generic `freezone_prepare_workflow` / `freezone_prepare_workflow_draft`: they
do not associate this saved material work with its execution draft. Missing or
rejected admission remains a failure; the actual native approval is still required.

Media generated in a separate workflow does not supply the native execution
receipt for a planned `source=generate` step. If the step has no complete receipt,
use the material preparation route for generation still needed. If the media
already exists, preserve its real current-canvas node instead of generating it
again: prepare a new work package with that step as `source=existing`, its actual
`node_id`, and no `recipe_id`. Preserve the applicable requirements and reference
relationships. Do not copy, invent, or transfer execution receipts. Review the
existing candidate normally: current project file, hash, media probe, and content
acceptance remain required; obtain user confirmation if content cannot be verified.

Read `freezone_get_production_progress` after actual completion notifications.
When the host returns a ready system check/adopt/bind action, use
`freezone_advance_production` with its current projection. Use the following
lower-level operations for inspecting specific candidates or an explicitly
requested manual step, while preserving the same selected work.
`freezone_review_material_candidate(work_ref,step_id,node_id)` checks current
project media and actual content where supported. A task-completed candidate
may be rejected or unverified. Accept with `freezone_accept_material_candidate`
only when both technical and content review passed, then bind only final
resources to the plan. `freezone_prepare_material_stage(work_ref)` prepares the
next ready wave. Pending upload steps remain visible until the user supplies
media. Use `freezone_associate_material_upload(work_ref,step_id,node_id)`
to attach the actually supplied current-canvas media to the pending upload
requirement, then review the candidate. Use `freezone_retry_material_step` with
a reason and stable command_id to invalidate the affected step and dependent
results; independent accepted work and earlier candidates remain recoverable.
Read the updated commission and work revisions before preparing the next wave.
Media binding or timing refinement retains approval of unchanged creative
content; changed target words, narrative or output requires a new content review.

For speech, inspect technically checked candidates before formal adoption.
Use only the host's registered `analyze_material_speech` action through the
existing remix processing/task framework, with the actual canvas_id, work_ref,
step_id, node_id and optional language. Keep the current node_revision and a
stable idempotency_key from that native envelope; scope, media path/hash,
provider identity and execution evidence are host resolved. This recognizes
the complete shared candidate once and saves `source_speech_observation.v1`.
Reuse its actual observation_ref for unchanged source/provider/language; a
content-pending candidate can be recognized but has not thereby passed review.
No-dialogue image/video material needs no ASR.

Read `freezone_get_material_speech_review(work_ref,step_id,node_id,observation_ref)`
for raw ASR, target dialogue differences and any saved correction_ref. The
Agent may inspect the textual difference and propose `{word_id,text,reason}`
corrections using the actual raw word IDs. When audio review is unavailable,
the authenticated user saves lexical corrections after listening in the native
candidate review. This read is not a sound approval or correction-save tool.
Keep the immutable raw ASR and measured times; saving a correction does not
accept the material, and changing target dialogue invalidates the old review.
Actual omitted words require material repair or a newly confirmed plan.
Do not claim to have listened when the host reports unavailable.

After genuine material acceptance and saved final/speech-source binding, use
the registered `adopt_source_observation` action with current plan_ref, take_id,
observation_ref, work_ref and optional actual saved correction_ref. Its native
envelope includes the actual canvas_id, current node_revision and stable
idempotency_key; host resolution owns scope, source hash, actor and receipt.
This maps already recognized words to the declared ordered shared take without
another ASR or audio approval. Preserve pauses and full source duration. If
the registered action is unavailable, keep its blocker and use the existing
registered full-source alignment only when no reusable observation exists.
For the established manual alignment route, read
`freezone_get_speech_review(plan_ref,take_id,recognition_ref,offset?,limit?)`.
The plan owns target words; raw ASR owns observations.
`freezone_prepare_speech_correction` accepts `{word_id,text,reason}` changes and
preserves measured time/word identities. Punctuation may be normalized. Until
an actual audio-review adapter is available, lexical changes require one
authenticated user confirmation of the shown original-to-corrected differences.
Do not claim to have listened when the host reports unavailable. Actual omitted
or wrong dialogue requires a new performed source or an explicitly changed
plan; fabricated SRT cannot repair it. Sentence-only evidence cannot authorize
a word-level effect. Shared speaking clips align once with ordered turns.

Use `freezone_get_production_progress` and `freezone_advance_production` for
current system progression. The host owns internal stage evidence and watches real saved
artifacts and task receipts and wakes a fresh turn after meaningful completion.
End the current turn while work runs, never poll endlessly. On recovery reuse
accepted resources and stored results, and reread current revisions before edits.

Respect production_scope: only-plan stops after plan confirmation. Editable
composition presents a kind="composition" decision with the exact manifest_ref
and compose_node_id so the user can inspect the current workbench. Requested
preview/export uses `freezone_review_production_output` after actual rendering,
checks native operation/media proof and presents playable output for complete
picture and sound review. Unobserved audio is not certified. An old render is
not current when adopted media or the saved plan changes.

### Complete plan drafting and historical outline continuation

`generate_production_plan_draft` generates and saves a complete editable `production_plan.v2` in one model call (`authoring_stage=arranged`), including structure, dialogue, resources and visual/audio arrangement, then ends its task. Read the current saved plan, review it against the user's goal, and use existing `production_plan_command` edits for bounded corrections; direction changes use public node update fields. Preserve existing canvas/action confirmation. A completed draft is not user confirmation or permission to generate media. Completion notifications must not submit another task; actual failures require explicit user authorization to retry.

Only for a historical saved outline (`authoring_stage=outline`) with visual/audio arrangement still pending, read the current plan and preserve its edits. When the user authorizes continuing, explicitly invoke the same node's `generate_production_plan_arrangement` through `freezone_run_node_action`, respecting the existing action confirmation. Its parameters are exactly `{"plan_ref":{"artifact_id":"CURRENT_SAVED_ID","revision":CURRENT_SAVED_REVISION}}`. Read the latest saved revision after edits; do not reconstruct source inputs or reuse internal outline files. This is a separate task with its own receipt. A failure preserves the outline and edits; only an explicit retry repeats arrangement. After actual arrangement, use the normal full plan review. The stage flag is informational: manually completed and historical plans still use existing readiness checks. No new stage cards or confirmation tools are needed.
