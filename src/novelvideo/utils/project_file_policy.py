"""Project internals require their typed, scoped APIs, never generic serving."""

from pathlib import Path, PurePosixPath
from posixpath import normpath


_INTERNAL_FREEZONE_NAMES = {
    '_remix_artifacts', '_remix_operations', '_production_index', '_production_epoch.json',
}


def is_internal_project_path(relative: str | Path) -> bool:
    parts = PurePosixPath(normpath(str(relative))).parts
    if len(parts) < 2 or parts[0].casefold() != 'freezone':
        return False
    name = parts[1].casefold()
    return name in _INTERNAL_FREEZONE_NAMES or name.startswith('_source_speech.sqlite3')
