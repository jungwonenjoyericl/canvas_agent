"""Reuse downloads only when Canvas metadata and the local bytes still match."""

import hashlib
from pathlib import Path

from .client import JsonObject
from .storage import read_json, write_json


def file_digest(path: Path) -> str:
    """Hash a local document without loading it all into memory."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _version(metadata: JsonObject, namespace: str) -> JsonObject:
    return {
        "namespace": namespace,
        **{
            key: metadata.get(key)
            for key in (
                "id",
                "updated_at",
                "modified_at",
                "size",
                "filename",
                "content-type",
            )
        },
    }


def reuse_download(
    path: Path, metadata: JsonObject, namespace: str, *, max_bytes: int
) -> bool:
    """Reject missing, edited, stale, oversized, or differently scoped downloads."""
    if not (metadata.get("updated_at") or metadata.get("modified_at")):
        return False
    saved = read_json(path.with_suffix(path.suffix + ".cache.json"))
    if not isinstance(saved, dict) or saved.get("version") != _version(
        metadata, namespace
    ):
        return False
    try:
        return path.stat().st_size <= max_bytes and saved.get("sha256") == file_digest(
            path
        )
    except FileNotFoundError:
        return False


def remember_download(path: Path, metadata: JsonObject, namespace: str) -> None:
    """Record successful download metadata, excluding signed URLs and credentials."""
    write_json(
        path.with_suffix(path.suffix + ".cache.json"),
        {"version": _version(metadata, namespace), "sha256": file_digest(path)},
    )
