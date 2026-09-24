# Generic remix bindings on the canvas

## Goal

The canvas must support remixing a reference video without assuming that every work has a person, product, or setting to replace. Script meaning, media production, and composition remain separate choices. Existing remix artifacts and completed media remain readable.

## Boundaries

- The script node owns dialogue, speaker turns, and semantic events. A dialogue event can have a replacement visual or sound, but a fixed replacement category is never required.
- A performance segment owns one accepted video or audio take. Its reference inputs are ordered bindings to existing canvas media nodes. Each binding has a user-authored purpose such as “keep this speaker's face” or “use this shot's camera movement”. Purpose is descriptive, not a schema enum.
- Video, image, and audio nodes own upload, model choice, prompts, generation, and media versions. Creating an input node from the script editor is a shortcut; generation stays in that node.
- The semantic timeline and compose node own timing, layer selection, and output. They must not require a global background, product, or scene reference when a performance take already supplies the picture and sound.
- Helpful batch actions apply one reference binding to any selected performance segments. A speaker-wide selection is one possible batch, not a different data model.

## Data and compatibility

- Add `reference_bindings: [{node_id, purpose}]` to a performance segment. The frontend uses `referenceBindings: [{nodeId, purpose}]`. Empty means no references and is valid.
- Existing `person_node_id`, `product_node_id`, `setting_node_id`, `motion_node_id`, and `voice_node_id` remain accepted for saved v1 artifacts. On read, adapt them into ordered bindings with the equivalent purpose. On the next edit, write the generic array; never discard a legacy binding silently.
- Reference node media type is determined by the node itself. The video node validates model-specific image, video, and audio limits. No product-specific field or provider port is introduced.
- A performance segment may optionally contain ordered `speech_ranges` inside its outer `start_char`/`end_char` envelope, allowing one generated or uploaded take to deliver several speaker turns without reading speaker labels aloud. Legacy segments use the outer range as their one speech range. Alignment uses concatenated spoken text and maps word offsets back to the original script ranges.
- Changing a binding invalidates only affected take selections. Saved scripts, media, and timeline artifacts retain immutable revisions; existing output can still be inspected.

## Interface

- Replace the three equal person/product/setting selectors with an “参考素材” list that is empty by default. Each row selects a canvas media node and states its purpose in plain language. Users can add or remove rows and apply a row to other chosen segments.
- Keep a compact “create upload node” and “create image node” shortcut; the resulting node opens for its own operation. Existing audio and video nodes can be selected as references.
- Keep “split by speaker” as an optional starting point. Offer merging consecutive turns into one take when the user wants a continuous exchange.
- Show factual script status (saved version, events, accepted takes) rather than one prescribed “next step”. Each node exposes its own actions.

## Verification

- Contract tests cover generic bindings, legacy adaptation, invalid binding content, and multi-turn speech ranges.
- Frontend tests cover optional references, arbitrary purpose, batch application, legacy data, and take creation from mixed media.
- Timing tests prove that one take spanning two role-labelled lines aligns only spoken words and maps semantic events to their original script offsets.
- Existing remix API, script editor, timeline, and composition tests remain green. Check both light and dark themes for the edited panel.
