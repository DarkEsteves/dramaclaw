"""Actor-scoped artifact store for production writes and reads."""

from __future__ import annotations

from novelvideo.freezone.remix.store import RemixArtifactStore
from novelvideo.project_context import ProjectContext


def production_store(ctx: ProjectContext) -> RemixArtifactStore:
    actor = ctx.requester_user_id.strip()
    if not actor:
        raise ValueError("production write requires a requester identity")
    return RemixArtifactStore(ctx.output_dir, ctx.project_id, created_by=actor)
