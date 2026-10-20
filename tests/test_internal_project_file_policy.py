from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from novelvideo.api.routes import files


INTERNAL_PATHS = [
    'freezone/_source_speech.sqlite3',
    'freezone/_source_speech.sqlite3-wal',
    'freezone/_source_speech.sqlite3-shm',
    'freezone/_remix_artifacts/private/rev_1.json',
    'freezone/_remix_operations/private.json',
    'freezone/_remix_operations/draft-stages/operation/task/outline.json',
    'freezone/_production_index/private/snapshot.json',
    'freezone/_production_epoch.json',
]


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['files', 'media', 'static'])
@pytest.mark.parametrize('relative', INTERNAL_PATHS)
async def test_project_viewer_cannot_download_internal_evidence(tmp_path, monkeypatch, surface, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'private Alice transcript')
    async def resolve(*args, **kwargs):
        assert kwargs['required_role'] == 'viewer'
        return SimpleNamespace(project_dir=tmp_path)
    monkeypatch.setattr(files, 'resolve_project_scope', resolve)
    with pytest.raises(HTTPException) as denied:
        if surface == 'files':
            await files.download_file('project', relative, {'username': 'bob'})
        elif surface == 'media':
            await files.preview_file('project', relative, Request({'type': 'http'}), user={'username': 'bob'})
        else:
            await files.preview_project_media_file('project', relative, {'username': 'bob'})
    assert denied.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('relative', ['videos/final.mp4', 'freezone/_outputs/task/video.mp4', 'freezone/remix_renders/final.mp4'])
async def test_public_project_video_remains_available(tmp_path, monkeypatch, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'public video')
    async def resolve(*args, **kwargs):
        return SimpleNamespace(project_dir=tmp_path)
    monkeypatch.setattr(files, 'resolve_project_scope', resolve)
    monkeypatch.setattr(files, '_serve_or_redirect_to_oss', lambda path, **kwargs: path)
    assert await files.download_file('project', relative, {}) == path
    assert await files.preview_project_media_file('project', relative, {}) == path


@pytest.mark.asyncio
async def test_symlink_alias_cannot_expose_internal_artifact(tmp_path, monkeypatch):
    private = tmp_path / 'freezone/_remix_artifacts/private.json'
    private.parent.mkdir(parents=True)
    private.write_bytes(b'private')
    (tmp_path / 'public.json').symlink_to(private)
    async def resolve(*args, **kwargs):
        return SimpleNamespace(project_dir=tmp_path)
    monkeypatch.setattr(files, 'resolve_project_scope', resolve)
    with pytest.raises(HTTPException) as denied:
        await files.download_file('project', 'public.json', {})
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_proxy_static_auth_rejects_internal_original_uri(monkeypatch):
    from novelvideo.api.routes import projects
    from unittest.mock import AsyncMock
    access = SimpleNamespace(resolve_requester_principals=AsyncMock(return_value=[]),
        effective_project_role_by_id=AsyncMock(return_value='viewer'))
    monkeypatch.setattr(projects, 'get_project_access', lambda: access)
    monkeypatch.setattr(projects, 'user_id_from_api_user', AsyncMock(return_value='bob'))
    request = Request({'type': 'http', 'headers': [(b'x-supertale-original-uri',
        b'/static/projects/project/freezone/%5Fremix_artifacts/private.json')]})
    with pytest.raises(HTTPException) as denied:
        await projects.authorize_project_static_media('project', user={}, request=request)
    assert denied.value.status_code == 403
