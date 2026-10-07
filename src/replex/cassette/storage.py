"""Reading and writing cassette files: atomic, deterministic, versioned."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from replex.cassette.model import SCHEMA_VERSION, Cassette
from replex.errors import CassetteError, CassetteNotFound, UnsupportedSchemaVersion

SUFFIX = ".replex.json"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-]+")


def sanitise_name(name: str) -> str:
    """Turn any string (for example a pytest node id) into a safe cassette file stem."""
    cleaned = _UNSAFE.sub("_", name.replace("::", "__")).strip("._")
    cleaned = re.sub(r"_{3,}", "__", cleaned)
    if not cleaned:
        raise CassetteError(f"cannot derive a cassette name from {name!r}")
    return cleaned[:200]


def cassette_path(directory: str | os.PathLike[str], name: str) -> Path:
    """Return ``<directory>/<name>.replex.json`` for a cassette name."""
    return Path(directory) / f"{sanitise_name(name)}{SUFFIX}"


def name_from_path(path: str | os.PathLike[str]) -> str:
    """Return the cassette name encoded in a file name."""
    base = Path(path).name
    return base[: -len(SUFFIX)] if base.endswith(SUFFIX) else Path(path).stem


def dumps(cassette: Cassette) -> str:
    """Serialise deterministically: sorted keys, two-space indent, trailing newline."""
    return dumps_data(cassette.model_dump(mode="json"))


def dumps_data(data: Any) -> str:
    """Deterministic JSON text for any JSON-compatible value."""
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def atomic_write_text(path: str | os.PathLike[str], text: str) -> Path:
    """Write ``text`` to ``path`` atomically (temp file in the same directory, then rename)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def save(cassette: Cassette, path: str | os.PathLike[str]) -> Path:
    """Write a cassette atomically and deterministically. Returns the path written."""
    return atomic_write_text(path, dumps(cassette))


def loads(text: str, *, source: str = "<string>") -> Cassette:
    """Parse and validate cassette JSON text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CassetteError(f"{source}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise CassetteError(f"{source}: a cassette must be a JSON object")
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"{source}: schema_version {version!r} is not supported by this release of "
            f"replex (supported: {SCHEMA_VERSION}). Upgrade replex or re-record the cassette."
        )
    try:
        return Cassette.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
        )
        raise CassetteError(f"{source}: invalid cassette ({problems})") from exc


def load(path: str | os.PathLike[str]) -> Cassette:
    """Load and validate a cassette file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CassetteNotFound(f"cassette not found: {p}") from exc
    return loads(text, source=str(p))


def list_cassettes(directory: str | os.PathLike[str]) -> list[Path]:
    """Every ``*.replex.json`` file directly inside ``directory``, sorted by name."""
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob(f"*{SUFFIX}") if not p.name.startswith("."))
