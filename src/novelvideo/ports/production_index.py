"""Optional deployment-owned index for scoped production display facts."""
from __future__ import annotations

from typing import Any, Protocol


class ProductionIndex(Protocol):
    def read_current(self, scope: dict, commission_id: str) -> dict[str, Any] | None: ...

    def publish(self, scope: dict, record: dict, *, expected_revision: str | None) -> dict: ...

    def history(self, scope: dict, commission_id: str, *, cursor: str | None, limit: int) -> dict: ...
