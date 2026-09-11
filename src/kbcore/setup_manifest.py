"""kbcore.setup_manifest — the CONTENT-derived version of the served hook assets.

Moved out of ``kbrest/server.py`` (wave S, KB 33784) so the nightly fleet
telemetry can compare a box's stamped ``hook_version`` against the digest the
server is CURRENTLY handing out — the line that would have shown on night one
that the H15 self-update reached nobody. The computation is byte-for-byte the
``/api/setup/manifest.json`` one (server.py's route is re-pointed here by the
integrator; ``tests/test_fleet_flush.py`` pins the two to the same answer).

Version is derived from the CONTENT, not hand-maintained: a digest over the
per-file digests. A version constant someone must remember to bump is a version
constant that silently goes stale, and the failure is invisible — a box reports
itself up to date against whatever it last fetched (2026-07-31).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Exactly the files install.sh needs. An allowlist, not a path join, so no
# route built on this can ever become a way to read arbitrary files. Kept in
# step with ``kbrest.server._SETUP_SCRIPTS`` by the agreement test.
SETUP_ASSETS = Path(__file__).resolve().parents[1] / "kbmcp" / "setup_assets"
SETUP_SCRIPTS = frozenset(
    {
        "install.sh",
        "flush-session-to-kb.sh",
        "kb-session-brief.sh",
        "kb-mcp-call.sh",
        "notify-complete.sh",
        "turn-start.sh",
        "kb-hook-dispatch.sh",
        "kb-flush-backfill.sh",
        "update.sh",
    }
)


def manifest() -> dict:
    """Content digests for every shipped setup asset plus the combined version.

    The same dict ``/api/setup/manifest.json`` serves: ``{"version": <12 hex>,
    "files": {name: sha256}, "count": n}``. A missing asset is simply absent
    from ``files`` (and so from the version), exactly as the route behaves.
    """
    files: dict[str, str] = {}
    for fname in sorted(SETUP_SCRIPTS):
        path = SETUP_ASSETS / fname
        if path.exists():
            files[fname] = hashlib.sha256(path.read_bytes()).hexdigest()
    combined = hashlib.sha256(
        "".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()
    ).hexdigest()
    return {"version": combined[:12], "files": files, "count": len(files)}


def version() -> str:
    """The 12-hex manifest version a current box stamps into ``.setup-version``."""
    return str(manifest()["version"])
