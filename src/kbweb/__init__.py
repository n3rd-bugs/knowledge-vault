"""kbweb — Phase 6b web UI over kbcore (Starlette + Jinja).

Invariant 11 (amended 2026-07-18): this package NEVER mutates knowledge — it
imports ONLY kbcore READ surfaces + render, never a knowledge-write function.
The sole mutating routes are the owner-only authorization-config actions in
views_admin (project-share grant/revoke via kbcore.principals; rw=false always).
Access is gated by a signed session cookie (see kbweb.auth).
"""
