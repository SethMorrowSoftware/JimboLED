"""Durable JSON writes.

Both files JimboLED owns have to survive the power simply going away: the
configuration, and the record of which emergency stops are latched.  Writing to
a temporary file and renaming it over the target is only half of that.  The
rename itself lives in the *directory*, and until the directory entry is on the
disk the old contents (or nothing at all) can come back after a power cut – so
the directory is fsynced too.

That matters most for ``estop.json``: "the latch survives a power cut" is a
promise about the one event most likely to happen while it is being written.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def fsync_dir(path: Path) -> None:
    """Flush a directory entry so a rename inside it survives a power cut."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - not all filesystems allow this
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - vfat and friends
        pass
    finally:
        os.close(fd)


def write_json_atomic(path: Path, data: Any, *, mode: int | None = None, **dump_kwargs) -> None:
    """Serialise ``data`` to ``path`` atomically and durably.

    Raises ``OSError`` if the file cannot be written; callers decide whether
    that is fatal.  The payload is serialised *before* anything on disk is
    touched, so an unserialisable value can never truncate a good file.
    """
    path = Path(path)
    payload = json.dumps(data, **dump_kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
        tmp_path = None
        fsync_dir(path.parent)
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:  # pragma: no cover
                pass
