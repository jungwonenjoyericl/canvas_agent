"""Atomic local writes shared by exports, snapshots, and download metadata."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_output(path: Path, content: str) -> None:
    """Replace a UTF-8 file only after writing its complete contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    """Read optional state; a missing or malformed file needs a fresh baseline."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, UnicodeError):
        return None


def write_json(path: Path, data: Any) -> None:
    """Persist JSON without partial files if the process or replacement fails."""
    write_output(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
