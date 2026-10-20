from types import SimpleNamespace
import json

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from novelvideo.api.reference_operation_logging import (
    ReferenceOperationRoute, log_plan_review_failure, validation_failure_record,
)


def test_validation_log_record_limits_fields_and_never_copies_request_body():
    request = SimpleNamespace(scope={"path_params": {"project": "p", "canvas_id": "c"},
                                     "endpoint": SimpleNamespace(__name__="material_strategy")})
    errors = [{"loc": ("body", "replacements", 0, "source"), "type": "literal_error",
               "input": "secret signed url?token=secret", "msg": "unexpected private content"}]
    record = validation_failure_record(request, errors)
    assert record == {"event": "reference_operation_failed", "project_id": "p",
                      "canvas_id": "c", "tool": "material_strategy", "error_code": "request_validation_failed",
                      "field_path": "body.replacements.0.source", "retryable": False}


def test_real_route_logs_422_field_without_private_request_value(caplog):
    class RequestBody(BaseModel):
        duration_seconds: int

    router = APIRouter(route_class=ReferenceOperationRoute)

    @router.post("/projects/{project}/canvases/{canvas_id}/generate")
    def generate(project: str, canvas_id: str, body: RequestBody):
        return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    private_value = "signed-url-secret-token"
    with caplog.at_level("WARNING", logger="novelvideo.api.reference_operation_logging"):
        response = TestClient(app).post("/projects/p/canvases/c/generate",
                                        json={"duration_seconds": private_value})
    assert response.status_code == 422
    records = [json.loads(record.message) for record in caplog.records
               if record.name == "novelvideo.api.reference_operation_logging"]
    assert len(records) == 1
    assert records[0]["field_path"] == "body.duration_seconds"
    assert records[0]["project_id"] == "p"
    assert private_value not in caplog.text


def test_route_conflict_logs_scope_and_error_code_without_detail(caplog):
    router = APIRouter(route_class=ReferenceOperationRoute)

    @router.post('/projects/{project}/canvases/{canvas_id}/work/{artifact_id}')
    def revise(project: str, canvas_id: str, artifact_id: str):
        raise HTTPException(409, {'code': 'artifact_revision_conflict',
                                  'secret': 'https://media.example/video?token=private'})

    app = FastAPI()
    app.include_router(router)
    with caplog.at_level('WARNING', logger='novelvideo.api.reference_operation_logging'):
        response = TestClient(app).post('/projects/p/canvases/c/work/work-a')
    assert response.status_code == 409
    record = json.loads(caplog.records[-1].message)
    assert record['artifact_id'] == 'work-a'
    assert record['error_code'] == 'artifact_revision_conflict'
    assert record['retryable'] is True
    assert 'token=private' not in caplog.text


def test_unexpected_route_error_logs_safe_stack_without_request_content(caplog):
    router = APIRouter(route_class=ReferenceOperationRoute)

    @router.post('/projects/{project}/canvases/{canvas_id}/review')
    def review(project: str, canvas_id: str):
        raise AttributeError('secret user text')

    app = FastAPI()
    app.include_router(router)
    with caplog.at_level('ERROR', logger='novelvideo.api.reference_operation_logging'):
        response = TestClient(app, raise_server_exceptions=False).post(
            '/projects/p/canvases/c/review', json={'private': 'another secret'},
        )
    assert response.status_code == 500
    record = json.loads(caplog.records[-1].message)
    assert record['project_id'] == 'p' and record['canvas_id'] == 'c'
    assert record['error_code'] == 'AttributeError'
    assert record['stack']
    assert record['request_id']
    assert 'secret' not in caplog.text


def test_plan_review_and_route_failure_share_request_id(caplog):
    router = APIRouter(route_class=ReferenceOperationRoute)

    @router.post('/projects/{project}/canvases/{canvas_id}/plan-review')
    def review(project: str, canvas_id: str):
        try:
            raise AttributeError('private user text')
        except AttributeError as error:
            log_plan_review_failure(project_id=project, canvas_id=canvas_id,
                                    commission_ref={'artifact_id': 'commission'},
                                    plan_ref={'artifact_id': 'plan'}, phase='evaluate', error=error)
            raise

    app = FastAPI()
    app.include_router(router)
    with caplog.at_level('ERROR', logger='novelvideo.api.reference_operation_logging'):
        response = TestClient(app, raise_server_exceptions=False).post(
            '/projects/p/canvases/c/plan-review', json={'private': 'another secret'},
        )
    assert response.status_code == 500
    records = [json.loads(row.message) for row in caplog.records]
    assert len(records) == 2
    assert records[0]['request_id'] == records[1]['request_id']
    assert records[0]['request_id']
