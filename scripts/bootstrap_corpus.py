"""Phase 3.5 corpus bootstrap (PLAN.md §7.6) — reproducible registry + reposcan.

Machine-specific to this server (paths under /home/usama/UpWork and
/backup/disk2/UpWork-Backup, surveyed 2026-07-02). Registers each project with
its domain + client-owned repos and runs the docs scanner over them. Live
checkouts win; backups fill only the projects absent live; vendor/upstream
clones and internal duplicates are excluded (never client IP).

Invariant 9 is enforced by kbworker.reposcan (git-tracked docs only,
symlink-safe, source-extension denylist), so this cannot ingest source files.
Idempotent: content-hash dedupe makes re-runs no-ops.

Run:  uv run python scripts/bootstrap_corpus.py

Curated non-git docs (BridgeSecuritySolution root planning MDs, PunchTech
notion-export) are staged separately into ~/kb/inbox/<project>/ for the
watcher — they live outside any git repo and so are not covered here.
"""

from __future__ import annotations

from pathlib import Path

from kbcore.items import set_project
from kbworker.reposcan import scan_repo

L = "/home/usama/UpWork"  # live checkouts (canonical)
B = "/backup/disk2/UpWork-Backup"  # backups (only for projects absent live)

# project -> (domain, [client-owned repo paths]). Excluded elsewhere: vendor
# clones (gstreamer, gst-plugins-rs, poky, meta-tegra, firesim-linux, uzlib,
# flipperzero, coremark/cJSON vendored subdirs), internal dups (mp-grouping-old,
# the extra smartaccess clones, cb212-c1005_server), and NESR's yocto/srsran
# upstream trees.
MAPPING: dict[str, tuple[str, list[str]]] = {
    "aquamonix": (
        "embedded",
        [
            f"{L}/Aquamonix/docker-mount/rm3",
            f"{L}/Aquamonix/docker-mount/rm3-config-server",
            f"{L}/Aquamonix/docker-mount/rm3-simulation",
            f"{L}/Aquamonix/docker-mount/mp-grouping",
        ],
    ),
    "blacksigma": ("web", [f"{L}/BlackSigma/git/cb212"]),
    "bridge-security": (
        "mixed",
        [
            f"{L}/BridgeSecuritySolution/bss-git/bss-backend",
            f"{L}/BridgeSecuritySolution/bss-git/bss-frontend",
            f"{L}/BridgeSecuritySolution/bss-git/bss-lib",
            f"{L}/BridgeSecuritySolution/bss-git/infrastructure",
            f"{L}/BridgeSecuritySolution/bss-git/rpi-firmware",
        ],
    ),
    "clairways": (
        "mixed",
        [
            f"{L}/Clairways/git/ConnectAdmin",
            f"{L}/Clairways/git/ConnectBase",
            f"{L}/Clairways/git/ConnectBeacon",
            f"{L}/Clairways/git/ConnectClient",
        ],
    ),
    "intensivate": (
        "embedded",
        [
            f"{L}/Intensivate/git/i-benchmarks",
            f"{L}/Intensivate/git/i-firemarshal",
        ],
    ),
    "pakal": (
        "embedded",
        [
            f"{L}/Pakal/git/pakal-ovenctrl",
            f"{L}/Pakal/git/pakal-ovenrtd",
            f"{L}/Pakal/git/thermalfixture",
        ],
    ),
    "phyllome": (
        "embedded",
        [
            f"{L}/Phyllome/git/eo-camera-image",
            f"{L}/Phyllome/git/pycam",
        ],
    ),
    "punchtech": ("embedded", [f"{L}/PunchTech/git/repeater"]),
    "unlimitedirl": ("devops", [f"{L}/UnlimitedIRL/git/opcm4-baseimage"]),
    "nesr": (
        "embedded",
        [
            f"{B}/NESR/git/at-interface",
            f"{B}/NESR/git/logger-radxa-rock3",
            f"{B}/NESR/git/oq-parser",
            f"{B}/NESR/git/sensor_daq_esp32",
        ],
    ),
    # backup-only (absent from the live root)
    "ims": ("mixed", [f"{B}/IMS"]),
    "kvm": ("embedded", [f"{B}/KVM"]),
    "sheetak": (
        "embedded",
        [
            f"{B}/Sheetak/git/cryosnap",
            f"{B}/Sheetak/git/SheetakDevKit",
        ],
    ),
    "smartaccess": ("embedded", [f"{B}/SmartAccess/git/smartaccess"]),
    "ccathletics": (
        "embedded",
        [
            f"{B}/CCAthletics/git/cablemate",
            f"{B}/CCAthletics/git/force_mate",
            f"{B}/CCAthletics/git/forcemate-hub",
            f"{B}/CCAthletics/git/forceplate_analysis_python",
        ],
    ),
}


def main() -> None:
    docs = dupes = errs = 0
    for project, (domain, repos) in MAPPING.items():
        present = [r for r in repos if (Path(r) / ".git").exists()]
        if not present:
            print(f"[{project}] no repos present on this host — skipped")
            continue
        set_project(project, domain=domain, repos=present)
        for repo in present:
            try:
                s = scan_repo(repo)
            except Exception as e:  # noqa: BLE001 — one bad repo must not stop the run
                print(f"[{project}] ERROR {Path(repo).name}: {e.__class__.__name__}: {e}")
                continue
            e = int(getattr(s, "errors", 0) or 0)
            docs, dupes, errs = docs + s.saved, dupes + s.skipped_dupes, errs + e
            print(f"[{project}] {Path(repo).name}: {s.saved} docs, {s.skipped_dupes} dup, {e} err")
    print(f"\nTOTAL: {docs} docs ingested, {dupes} dupes, {errs} per-file errors")


if __name__ == "__main__":
    main()
