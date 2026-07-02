"""kbweb — Phase 6b read-only web UI over kbcore (Starlette + Jinja).

Invariant 11: this package imports ONLY kbcore READ surfaces + render — never a
write function. Access is gated by a signed session cookie (see kbweb.auth).
"""
