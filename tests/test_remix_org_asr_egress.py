"""Organization Remix ASR uses the durable credential and egress ports."""

from pathlib import Path

import httpx
import pytest

from novelvideo.egress_context import TrustedEgressContext
from novelvideo.model_gateway_runtime import model_gateway_request_scope
from novelvideo.ports.authz import BillingPrincipal
from novelvideo.ports.egress_operations import (
    HandleKind,
    OperationClaimResult,
    OperationSnapshot,
    OperationState,
)
from novelvideo.ports.model_credentials import CredentialReference, RequestCredential
from novelvideo.ports.model_credentials import ModelCredentialError


def _organization_context() -> TrustedEgressContext:
    return TrustedEgressContext(
        envelope_id="envelope-asr", project_id="project-1", task_type="remix_transcribe_media",
        requester_user_id="user-1", root_task_id="task-1", admission_id="admission-1",
        admitted_at="2026-10-12T00:00:00Z", membership_id="membership-1", authz_version=2,
        billing_principal=BillingPrincipal(kind="organization", id="org-1"),
        credential=CredentialReference(
            source="organization", credential_id="org-key-1", key_version=3, org_id="org-1"
        ),
    )


class _Operations:
    def __init__(self, *, replay: bool = False) -> None:
        self.events = []
        self.specs = []
        self.replay = replay

    async def claim(self, *, spec):
        self.events.append("claim")
        self.specs.append(spec)
        return OperationClaimResult(
            won=not self.replay,
            operation=OperationSnapshot("op-1", spec.operation_key, OperationState.DISPATCHING, 1),
            transition_token=None if self.replay else "token-1",
        )

    async def mark_rejected_before_submit(self, **kwargs):
        self.events.append("rejected")

    async def mark_unknown(self, **kwargs):
        self.events.append("unknown")

    async def mark_accepted(self, **kwargs):
        self.events.append(("accepted", kwargs["provider_job_id"]))
        return OperationSnapshot("op-1", "key-1", OperationState.ACCEPTED, 2)

    async def mark_completed(self, **kwargs):
        self.events.append(("completed", kwargs["result_ref"]))


class _Credentials:
    def __init__(self, context, events):
        self.context = context
        self.events = events

    async def resolve(self, admission):
        self.events.append("resolve")
        assert admission.credential == self.context.credential
        return RequestCredential(
            reference=self.context.credential,
            api_key="organization-secret", base_url="https://org-gateway.example/v1",
        )


def test_organization_asr_claims_before_resolving_and_submits_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.freezone.remix import alignment, newapi_transcription
    from novelvideo import model_gateway_runtime as runtime

    context = _organization_context()
    operations = _Operations()
    credentials = _Credentials(context, operations.events)
    monkeypatch.setattr(runtime, "get_egress_operation_port", lambda: operations)
    monkeypatch.setattr(runtime, "get_model_credentials", lambda: credentials)
    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))

    def respond(request):
        operations.events.append("post")
        assert request.url.host == "org-gateway.example"
        assert request.headers["authorization"] == "Bearer organization-secret"
        return httpx.Response(200, json={"language": "english", "text": "hi", "words": [{"word": "hi", "start": 0.1, "end": 0.3}]})

    original_client = httpx.Client
    monkeypatch.setattr(newapi_transcription.httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), timeout=kwargs["timeout"]))
    media = tmp_path / "speech.wav"
    media.write_bytes(b"audio")
    with model_gateway_request_scope(context):
        result = alignment.transcribe_media(media, "en")

    assert result["transcript"] == "hi"
    assert operations.events == ["claim", "resolve", "post", ("accepted", None), ("completed", None)]
    assert operations.specs[0].handle_kind is HandleKind.NONE
    assert operations.specs[0].credential_id == "org-key-1"


def test_organization_asr_replay_never_resolves_or_posts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.freezone.remix import alignment
    from novelvideo import model_gateway_runtime as runtime

    context = _organization_context()
    operations = _Operations(replay=True)
    monkeypatch.setattr(runtime, "get_egress_operation_port", lambda: operations)
    monkeypatch.setattr(runtime, "get_model_credentials", lambda: (_ for _ in ()).throw(AssertionError("credential resolution")))
    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))
    media = tmp_path / "speech.wav"
    media.write_bytes(b"audio")

    with model_gateway_request_scope(context), pytest.raises(runtime.ModelGatewayEgressError) as error:
        alignment.transcribe_media(media, "en")
    assert error.value.code == "EGRESS_OPERATION_REPLAYED"
    assert operations.events == ["claim"]


def test_organization_provider_signature_tracks_credential_version_without_platform_lookup(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace
    from novelvideo.freezone.remix.source_speech import configured_provider_signature

    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))
    context = _organization_context()
    with model_gateway_request_scope(context):
        first = configured_provider_signature()
    with model_gateway_request_scope(replace(context, credential=replace(context.credential, key_version=4))):
        second = configured_provider_signature()
    assert first != second
    assert "organization-secret" not in first
    assert "org-key-1" not in first


def test_organization_credential_failure_rejects_before_submit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.freezone.remix import alignment
    from novelvideo import model_gateway_runtime as runtime

    operations = _Operations()
    monkeypatch.setattr(runtime, "get_egress_operation_port", lambda: operations)

    class MissingCredential:
        async def resolve(self, admission):
            operations.events.append("resolve")
            raise ModelCredentialError("ORG_CREDENTIAL_DISABLED")

    monkeypatch.setattr(runtime, "get_model_credentials", lambda: MissingCredential())
    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))
    media = tmp_path / "speech.wav"
    media.write_bytes(b"audio")
    with model_gateway_request_scope(_organization_context()), pytest.raises(ModelCredentialError) as error:
        alignment.transcribe_media(media, "en")
    assert error.value.code == "ORG_CREDENTIAL_DISABLED"
    assert operations.events == ["claim", "resolve", "rejected"]


def test_organization_asr_rejects_oversized_media_before_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.freezone.remix import alignment, newapi_transcription
    from novelvideo import model_gateway_runtime as runtime

    monkeypatch.setattr(newapi_transcription, "MAX_ORGANIZATION_AUDIO_BYTES", 4, raising=False)
    monkeypatch.setattr(runtime, "get_egress_operation_port", lambda: (_ for _ in ()).throw(AssertionError("claim")))
    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))
    media = tmp_path / "speech.wav"
    media.write_bytes(b"12345")
    with model_gateway_request_scope(_organization_context()), pytest.raises(newapi_transcription.TranscriptionPreDispatchError):
        alignment.transcribe_media(media, "en")


def test_organization_asr_accepts_size_limit_before_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novelvideo.freezone.remix import alignment, newapi_transcription
    from novelvideo import model_gateway_runtime as runtime

    monkeypatch.setattr(newapi_transcription, "MAX_ORGANIZATION_AUDIO_BYTES", 4, raising=False)
    operations = _Operations(replay=True)
    monkeypatch.setattr(runtime, "get_egress_operation_port", lambda: operations)
    media = tmp_path / "speech.wav"
    media.write_bytes(b"1234")
    with model_gateway_request_scope(_organization_context()), pytest.raises(runtime.ModelGatewayEgressError):
        alignment.transcribe_media(media, "en")
    assert operations.events == ["claim"]


@pytest.mark.parametrize("failure", ["file_open", "client_setup"])
def test_organization_asr_local_setup_failure_is_rejected_before_submit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    from novelvideo.freezone.remix import alignment, newapi_transcription
    from novelvideo import model_gateway_runtime as runtime

    context = _organization_context()
    operations = _Operations()
    monkeypatch.setattr(runtime, "get_egress_operation_port", lambda: operations)
    monkeypatch.setattr(runtime, "get_model_credentials", lambda: _Credentials(context, operations.events))
    media = tmp_path / "speech.wav"
    media.write_bytes(b"audio")

    if failure == "file_open":
        original_open = Path.open
        opens = 0

        def open_file(path, *args, **kwargs):
            nonlocal opens
            if path == media:
                opens += 1
                if opens == 2:
                    raise PermissionError("local file inaccessible")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_file)
    else:
        def broken_client(**_kwargs):
            raise OSError("local client setup failed")

        monkeypatch.setattr(newapi_transcription.httpx, "Client", broken_client)

    with model_gateway_request_scope(context), pytest.raises(newapi_transcription.TranscriptionPreDispatchError):
        alignment.transcribe_media(media, "en")
    assert operations.events == ["claim", "resolve", "rejected"]


def test_alignment_rejects_provider_sentence_that_disagrees_with_timed_words(
    tmp_path: Path
) -> None:
    from novelvideo.freezone.remix.alignment import align_script
    from novelvideo.freezone.remix.newapi_transcription import NewApiSpeechTranscriber

    media = tmp_path / "speech.wav"
    media.write_bytes(b"audio")
    transcriber = NewApiSpeechTranscriber(
        api_key="test-key", base_url="https://gateway.example/v1",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
            "language": "english", "text": "hello world",
            "words": [
                {"word": "hello", "start": 0.1, "end": 0.5},
                {"word": "there", "start": 0.6, "end": 1.0},
            ],
        })),
    )

    with pytest.raises(RuntimeError, match="disagrees with timed words"):
        align_script(media, {"revision": 1, "dialogue": "hello there", "language": "en"}, aligner=transcriber)


@pytest.mark.parametrize("sentence", ["hello world there", "hello there world"])
def test_alignment_rejects_untimed_provider_sentence_words(
    tmp_path: Path, sentence: str
) -> None:
    from novelvideo.freezone.remix.alignment import align_script
    from novelvideo.freezone.remix.newapi_transcription import NewApiSpeechTranscriber

    media = tmp_path / "speech.wav"
    media.write_bytes(b"audio")
    transcriber = NewApiSpeechTranscriber(
        api_key="test-key", base_url="https://gateway.example/v1",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
            "language": "english", "text": sentence,
            "words": [
                {"word": "hello", "start": 0.1, "end": 0.5},
                {"word": "there", "start": 0.6, "end": 1.0},
            ],
        })),
    )

    with pytest.raises(RuntimeError, match="untimed text"):
        align_script(media, {"revision": 1, "dialogue": "hello there", "language": "en"}, aligner=transcriber)


@pytest.mark.asyncio
async def test_organization_readiness_uses_credential_port_without_platform_fallback(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from novelvideo.api.routes import remix
    import novelvideo.ports as ports

    context = _organization_context()
    events = []

    class DisabledCredential:
        async def resolve(self, admission):
            events.append(admission.credential)
            raise ModelCredentialError("ORG_CREDENTIAL_DISABLED")

    monkeypatch.setattr(ports, "get_model_credentials", lambda: DisabledCredential())
    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))
    assert await remix._transcription_engine_ready_for_context(context) is False
    assert events == [context.credential]

    monkeypatch.setattr(ports, "get_model_credentials", lambda: _Credentials(context, []))
    assert await remix._transcription_engine_ready_for_context(context) is True


@pytest.mark.asyncio
async def test_organization_readiness_route_reports_org_key_without_platform_key(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from novelvideo.api.routes import remix
    import novelvideo.ports as ports

    context = _organization_context()

    async def admitted(*_args, **_kwargs):
        return context

    monkeypatch.setattr(remix, "_request_transcription_context", admitted)
    monkeypatch.setattr(ports, "get_model_credentials", lambda: _Credentials(context, []))
    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))
    response = await remix.read_transcription_readiness("project-1", {"user_id": "user-1"})
    assert response == {"ok": True, "data": {"ready": True, "engine": "newapi", "model": "audio-transcribe"}}


@pytest.mark.asyncio
async def test_organization_action_preflight_does_not_fall_back_to_platform(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException
    from novelvideo.api.routes import remix
    import novelvideo.ports as ports

    context = _organization_context()

    async def admitted(*_args, **_kwargs):
        return context

    async def store(*_args, **_kwargs):
        return object()

    class DisabledCredential:
        async def resolve(self, _admission):
            raise ModelCredentialError("ORG_CREDENTIAL_DISABLED")

    monkeypatch.setattr(remix, "_store", store)
    monkeypatch.setattr(remix, "_request_transcription_context", admitted)
    monkeypatch.setattr(ports, "get_model_credentials", lambda: DisabledCredential())
    monkeypatch.setattr("novelvideo.config.get_newapi_runtime_credentials", lambda: (_ for _ in ()).throw(AssertionError("platform credentials read")))
    body = remix.ProcessActionBody(
        action="transcribe_media", payload={}, idempotency_key="one", node_revision=1,
    )
    with pytest.raises(HTTPException) as error:
        await remix.enqueue_process_action("project-1", body, {"user_id": "user-1"})
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_material_speech_status_reads_org_provider_signature(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from novelvideo.api.routes import reference_material
    from novelvideo.api import egress_binding
    from novelvideo.freezone.remix import candidate_speech_correction
    from novelvideo.model_gateway_runtime import current_model_gateway_context

    context = _organization_context()

    async def admitted(**_kwargs):
        return context

    async def scoped(*_args, **_kwargs):
        return SimpleNamespace(requester_user_id="user-1", project_id="project-1"), {}, object(), {}

    def status(*_args, **_kwargs):
        return {"credential": current_model_gateway_context().credential.credential_id}

    monkeypatch.setattr(egress_binding, "build_request_egress_context", admitted)
    monkeypatch.setattr(reference_material, "_scope", scoped)
    monkeypatch.setattr(candidate_speech_correction, "find_candidate_speech_review", status)
    body = reference_material.MaterialSpeechStatusBody(
        work_ref={"artifact_id": "w", "revision": 1}, step_id="step", node_id="node"
    )
    result = await reference_material.material_speech_status("project-1", "canvas", body, {})
    assert result["data"] == {"credential": "org-key-1"}


@pytest.mark.asyncio
async def test_material_speech_reconciliation_uses_org_provider_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace
    from novelvideo.api.routes import remix
    from novelvideo.freezone.remix import source_speech
    from novelvideo.model_gateway_runtime import current_model_gateway_context

    context = _organization_context()
    record = {"producer_version": "remix-source-speech.v1", "status": "pending_reconciliation"}

    async def store(*_args, **_kwargs):
        return SimpleNamespace(project_dir=tmp_path, project_id="project-1")

    async def admitted(*_args, **_kwargs):
        return context

    async def project_context(**_kwargs):
        return SimpleNamespace(project_id="project-1", requester_user_id="user-1")

    async def authorize(*_args, **_kwargs):
        return None

    class Operations:
        def __init__(self, *_args):
            pass

        def get(self, _operation_id):
            return record

    def reconcile(_ctx, _record):
        assert current_model_gateway_context() == context
        return None

    monkeypatch.setattr(remix, "_store", store)
    monkeypatch.setattr(remix, "_request_transcription_context", admitted)
    monkeypatch.setattr(remix, "resolve_project_context", project_context)
    monkeypatch.setattr(remix, "_authorize_material_speech_operation", authorize)
    monkeypatch.setattr(remix, "RemixOperationStore", Operations)
    monkeypatch.setattr(remix, "_operation_view", lambda value: value)
    monkeypatch.setattr(source_speech, "reconcile_material_speech", reconcile)
    result = await remix.reconcile_operation("project-1", "operation-1", {})
    assert result["data"] == record
