# Freezone chat text and operation outcomes

Freezone assistant prose uses the ordinary accumulated `assistant.delta` stream. The
Codex canvas turn has no final `message/mode/canvas_receipts` JSON envelope and
no format repair turn. The complete text is persisted once in the assistant
message; reconnecting does not append the streamed prefix twice.

Every canvas or artifact write is evaluated from host observed tool events.
`runtime_event_evidence.py` checks the exact actor, project, canvas, revision
and persisted receipt. A successful transport response alone is `unverified`.
A draft awaiting the existing browser approval is `awaiting_approval`; an
accepted background task is `accepted` or `running`, never completed media.
Failures, timeouts and cancellation retain their own call IDs and errors. A
separate successful operation cannot hide one of these results.

The assistant message stores `metadata.operation_outcome` with the per call
state, `blocking_call_ids` and turn disposition. The existing tool progress and
canvas execution UI show these states. No extra confirmation card is created.
A complete prose reply, a successful write, and presentation of a reviewed
plan are separate events. `plan_confirmation` is recorded only after the full
plan reply is delivered and no blocking operation remains.

Natural language cannot be validated by a final JSON receipt. Agent guidance
therefore requires accurate wording: intent before submission, submitted after
a verified acceptance, and generated only after a terminal artifact exists.
The UI and workflow advance from host receipts, never from the assistant's
words. Existing canvas write and node action approval policies still decide
when commands actually apply. The Freezone thread protocol version changes
when these developer instructions change; existing saved chats remain readable.
