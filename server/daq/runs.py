"""Recorded runs on disk.

A run is a directory holding one wave file per channel plus its metadata, so a
run can be listed, downloaded and deleted as a single thing. Runs live outside
the source tree - the app may be running from a read-only mount, and data should
not land in the repo either way.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, asdict

DATA_ROOT = os.path.abspath(os.environ.get(
    "DAQ_DATA_DIR", os.path.join(os.path.expanduser("~"), "daq-runs")))

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(name: str) -> str:
    """A filesystem-safe stem. Empty or hostile input still yields something."""
    s = _SAFE.sub("-", (name or "").strip()).strip("-._")
    return s[:60] or "run"


@dataclass
class Run:
    id: str                 # the directory name: the run's one and only name
    started: float          # unix seconds
    files: int
    bytes: int
    channels: list[int]
    events: int | None = None
    # The operator's note from record time - what was tested, beam energy -
    # so the listing can answer "which run was that?" without opening files.
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _root() -> str:
    os.makedirs(DATA_ROOT, exist_ok=True)
    return DATA_ROOT


_RUN_FILE = re.compile(r"^run_(\d+)\.root$", re.IGNORECASE)


def next_run_number() -> int:
    """One more than the highest run number anywhere in the data directory.

    The number lives both in each run's metadata and in its run_<N>.root
    filename; both are scanned, so a directory of files copied in from another
    machine still counts. Max-based, so deleting old runs never causes a
    number to be reused - a run number must never mean two different datasets.
    """
    top = 0
    try:
        entries = list(os.scandir(_root()))
    except OSError:
        return 1
    for entry in entries:
        if not entry.is_dir():
            continue
        n = _read_meta(entry.path).get("run_number")
        if isinstance(n, int):
            top = max(top, n)
        try:
            for f in os.scandir(entry.path):
                m = _RUN_FILE.match(f.name)
                if m:
                    top = max(top, int(m.group(1)))
        except OSError:
            continue
    return top + 1


def create(name: str, timestamp: bool = True) -> tuple[str, str]:
    """Make a fresh run directory. Returns (run_name, path).

    The directory name IS the run's name - the one that appears in the listing,
    in the metadata and on the downloaded zip. With `timestamp` it gets an
    ISO-ish suffix, which is what keeps two runs of the same name apart; without
    it, a clash is an error rather than a silent rename.
    """
    base = slug(name)
    if timestamp:
        base = f"{base}-{time.strftime('%Y-%m-%d-%H%M%S')}"
    path = os.path.join(_root(), base)
    if os.path.exists(path):
        raise FileExistsError(base)
    os.makedirs(path)
    return base, path


def path_of(run_id: str) -> str | None:
    """Resolve an id to a directory, refusing anything outside the data root."""
    if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
        return None
    p = os.path.abspath(os.path.join(_root(), run_id))
    if os.path.dirname(p) != os.path.abspath(_root()) or not os.path.isdir(p):
        return None
    return p


def _read_meta(path: str) -> dict:
    try:
        with open(os.path.join(path, "run_metadata.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def describe(run_id: str) -> Run | None:
    path = path_of(run_id)
    if path is None:
        return None
    meta = _read_meta(path)
    files = 0
    total = 0
    with os.scandir(path) as entries:
        for entry in entries:
            try:
                if entry.is_file():
                    files += 1
                    total += entry.stat().st_size
            except OSError:
                continue          # written to, or removed, while we looked
    chans = sorted(int(c) for c in (meta.get("channels") or {}))
    return Run(id=run_id,
               started=meta.get("started", os.path.getmtime(path)),
               files=files, bytes=total, channels=chans,
               events=meta.get("events"),
               note=str(meta.get("note") or ""))


def listing() -> list[dict]:
    """Newest first, by the start time in the metadata.

    Sorting by directory name only looks right while every run carries a
    timestamp suffix; a run recorded without one sorted wherever its letters
    fell, under a heading that says newest first.
    """
    out = []
    with os.scandir(_root()) as entries:
        names = [e.name for e in entries if e.is_dir()]
    for name in names:
        r = describe(name)
        if r:
            out.append(r.to_dict())
    out.sort(key=lambda r: (r["started"], r["id"]), reverse=True)
    return out


def discard_empty(run_id: str) -> bool:
    """Remove a run directory that was created but never written to.

    A run whose files could not be opened would otherwise sit in the listing
    looking like a recording that happened and produced nothing.
    """
    path = path_of(run_id)
    if path is None:
        return False
    with os.scandir(path) as entries:
        if any(True for _ in entries):
            return False
    try:
        os.rmdir(path)
    except OSError:
        return False
    return True


def delete(run_id: str) -> bool:
    """True if it is gone. Raises OSError if the filesystem refused."""
    path = path_of(run_id)
    if path is None:
        return False
    shutil.rmtree(path)
    return True


def zip_to_temp(run_id: str) -> str | None:
    """Zip a run into a temp file and return its path; the caller unlinks it."""
    path = path_of(run_id)
    if path is None:
        return None
    with os.scandir(path) as entries:
        files = sorted((e.name, e.path) for e in entries if e.is_file())
    fd, tmp = tempfile.mkstemp(suffix=".zip", prefix=f"{run_id}_")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for name, src in files:
                z.write(src, arcname=os.path.join(run_id, name))
    except BaseException:
        # A half-written zip is worse than none: it downloads and then fails to
        # open. Take the partial file with us on the way out.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp
