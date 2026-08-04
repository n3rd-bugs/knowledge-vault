"""Package O — observability.

Two independent signals, tested here:

1. 401-BURST ALERTING (:mod:`kbrest.auth_alert`). The public surface is silent on
   failed auth by design (no-oracle 401), so a stale-hooks box / broken installer
   probing the gate produced no signal. An observe-only ASGI wrapper now counts
   401s in a sliding 10-min window and pushes ONE rate-limited ntfy line when a
   window crosses ``kb_auth_alert_threshold`` — WITHOUT changing the 401 response
   and WITHOUT ever blocking a request (fire-and-forget on a daemon thread).

2. EXAMINED-BEFORE-EXPIRY (:func:`kbcore.review_auto.curation_counts` +
   :func:`kbworker.tail_steps.digest_lines`). The quiet-expiry cliff used to be
   reconstructed by hand; the untriaged-eligible pool it drains is now first-class
   in the nightly report, with a digest line when rows expire within 7 days.

DB tests run against kb_test (conftest ``kb``); the counter/middleware/digest
tests are pure and touch neither DB nor network.
"""

from __future__ import annotations

import asyncio
import threading
import types

from kbcore import db, review_auto, review_triage
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.review import queue_review
from kbrest import auth_alert
from kbworker import tail_steps

_TOKEN = "kbtest-" + "0" * 32


# ============================================================ 1. 401-burst alert
# --- the counter (pure; deterministic clock, no threads/sockets) --------------


def _counter(monkeypatch, threshold: int) -> auth_alert.AuthAlertCounter:
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", threshold)
    return auth_alert.AuthAlertCounter()


def test_under_threshold_is_silent(monkeypatch):
    c = _counter(monkeypatch, 20)
    for i in range(19):  # one shy of the default threshold
        assert c.record("10.0.0.1", 1000.0 + i) is None


def test_threshold_crossing_fires_exactly_once(monkeypatch):
    c = _counter(monkeypatch, 5)
    for i in range(4):
        assert c.record(f"10.0.0.{i}", 1000.0 + i) is None
    assert c.record("10.0.0.9", 1004.0) == "AUTH: 5 failed-auth in 10min from 5 IPs"
    # every later 401 inside the hour is rate-limited to silence
    for i in range(5, 40):
        assert c.record("10.0.0.9", 1000.0 + i) is None


def test_kill_switch_never_alerts_and_keeps_no_state(monkeypatch):
    c = _counter(monkeypatch, 0)
    for i in range(100):
        assert c.record("2.2.2.2", 1000.0 + i) is None
    assert len(c._events) == 0, "threshold=0 must not even retain events"


def test_old_events_fall_out_of_the_ten_minute_window(monkeypatch):
    c = _counter(monkeypatch, 5)
    for i in range(4):
        c.record("3.3.3.3", 1000.0 + i)
    # 11 minutes later the four old events are pruned; four fresh ones stay < 5
    for i in range(4):
        assert c.record("3.3.3.3", 1000.0 + 660 + i) is None
    assert len(c._events) == 4


def test_alert_line_counts_distinct_client_ips(monkeypatch):
    c = _counter(monkeypatch, 4)
    line = None
    for n, ip in enumerate(("a", "b", "a", "b")):  # 4 events, 2 distinct IPs
        line = c.record(ip, 1000.0 + n)
    assert line == "AUTH: 4 failed-auth in 10min from 2 IPs"


def test_sustained_probe_alerts_at_most_once_per_hour(monkeypatch):
    """A probe hammering the gate for over an hour must alert at most hourly."""
    c = _counter(monkeypatch, 3)
    fired = [(k, line) for k in range(0, 70 * 60, 10) if (line := c.record("9.9.9.9", 1000.0 + k))]
    assert [k for k, _ in fired] == [20, 3620], "one alert per hour, not one per window"


# --- the middleware (ASGI passthrough + off-path fire-and-forget) -------------

_401_MESSAGES = [
    {
        "type": "http.response.start",
        "status": 401,
        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
    },
    {"type": "http.response.body", "body": b'{"error": "unauthorized"}'},
]


def _app_sending(messages):
    async def app(scope, receive, send):
        for m in messages:
            await send(m)

    return app


def _run(mw, client_ip: str = "203.0.113.7") -> list[dict]:
    out: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(m):
        out.append(m)

    asyncio.run(mw({"type": "http", "client": (client_ip, 4444), "headers": []}, receive, send))
    return out


def test_middleware_forwards_the_401_byte_for_byte(monkeypatch):
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 10_000)  # far above any count
    mw = auth_alert.AuthAlertMiddleware(_app_sending(_401_MESSAGES))
    assert _run(mw) == _401_MESSAGES, "the no-oracle 401 contract must be untouched"


def test_alert_fires_on_a_separate_thread_off_the_request_path(monkeypatch):
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 1)
    seen: dict = {}
    done = threading.Event()

    def capture(line):
        seen["thread"] = threading.get_ident()
        seen["line"] = line
        done.set()

    monkeypatch.setattr(auth_alert, "push_alert", capture)
    mw = auth_alert.AuthAlertMiddleware(_app_sending(_401_MESSAGES))

    assert _run(mw) == _401_MESSAGES, "the request is served regardless of the push"
    assert done.wait(timeout=5), "the alert was never dispatched"
    assert seen["line"] == "AUTH: 1 failed-auth in 10min from 1 IPs"
    assert seen["thread"] != threading.get_ident(), "the push must not run on the request thread"


def test_non_401_responses_are_never_counted(monkeypatch):
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 1)
    fired: list[str] = []
    monkeypatch.setattr(auth_alert, "push_alert", fired.append)
    ok = [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"{}"},
    ]
    mw = auth_alert.AuthAlertMiddleware(_app_sending(ok))
    for _ in range(5):
        assert _run(mw) == ok
    assert fired == [], "only 401s count toward the burst"


# --- end-to-end: the counter is actually installed in build_app --------------


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously on start(),
    so the fire-and-forget push is observable without real thread scheduling."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def test_build_app_installs_the_counter_end_to_end(kb, monkeypatch):
    """Unauthenticated requests to the real REST app cross the threshold and fire
    exactly one rate-limited alert; a valid request (200) never counts."""
    monkeypatch.setattr(settings, "kb_token", _TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    monkeypatch.setattr(settings, "kb_web_public_url", "")  # no CORS wrap; deterministic
    monkeypatch.setattr(settings, "kb_auth_alert_threshold", 3)
    pushed: list[str] = []
    monkeypatch.setattr(auth_alert, "push_alert", pushed.append)
    # Run the fire-and-forget dispatch inline so the assertions are race-free,
    # without touching the real threading module.
    monkeypatch.setattr(
        auth_alert,
        "threading",
        types.SimpleNamespace(Thread=_InlineThread),
    )
    from starlette.testclient import TestClient

    from kbrest.server import build_app

    client = TestClient(build_app())
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert pushed == [], "2 < 3 is still silent"
    # a valid, authenticated call is a 200 and must NOT advance the counter
    authed = client.get("/api/projects", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert authed.status_code == 200
    assert client.get("/api/stats").status_code == 401  # the 3rd 401 crosses
    assert len(pushed) == 1 and pushed[0].startswith("AUTH: 3 failed-auth in 10min")
    for _ in range(5):  # a sustained probe does not spam
        client.get("/api/stats")
    assert len(pushed) == 1


# ==================================================== 2. examined-before-expiry
# --- seed helpers -------------------------------------------------------------


def _unverified(project: str, tag: str) -> tuple[int, int]:
    item_id = ingest(project, "note", f"n {tag}", f"b {tag}", trust="unverified", uri=tag)
    return item_id, queue_review(item_id, "unverified_extraction", {"rejected_count": 0})


def _age_created(queue_id: int, days: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, queue_id),
        )


def _age_triage(queue_id: int, days: int) -> None:
    """Backdate a stored triage suggestion's timestamp (it left the pool long ago)."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE review_queue SET detail = jsonb_set(detail, '{triage,at}', "
            "to_jsonb((now() - make_interval(days => %s))::text)) WHERE id = %s",
            (days, queue_id),
        )


def _record_access(item_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("INSERT INTO access_log (item_id, tool) VALUES (%s, 'test')", (item_id,))


# --- the counts ---------------------------------------------------------------


def test_untriaged_expiring_counts_over_a_mixed_corpus(kb):
    # A: untriaged, 25d old  -> expires in ~5d  (within 7d and 30d)
    _a, qa = _unverified("obs", "A")
    _age_created(qa, 25)
    # B: untriaged, 10d old  -> expires in ~20d (within 30d only)
    _b, qb = _unverified("obs", "B")
    _age_created(qb, 10)
    # C: untriaged, fresh    -> expires in 30d  (within 30d only, at the boundary)
    _c, _qc = _unverified("obs", "C")
    # D: 25d old but KEEP-triaged  -> examined, excluded from the at-risk counts
    _d, qd = _unverified("obs", "D")
    _age_created(qd, 25)
    review_triage.store_triage(qd, {"action": "keep", "dup_of": None, "rationale": "r"})
    # E: 25d old, REJECT-triaged   -> known-bad, exempt from quiet expiry entirely
    _e, qe = _unverified("obs", "E")
    _age_created(qe, 25)
    review_triage.store_triage(qe, {"action": "reject_noise", "dup_of": None, "rationale": "r"})
    # F: 25d old, untriaged, but RECALLED -> not quiet-expiry-eligible (has access)
    fid, qf = _unverified("obs", "F")
    _age_created(qf, 25)
    _record_access(fid)

    counts = review_auto.curation_counts(30)
    assert counts["untriaged_expiring_7d"] == 1, "only A is untriaged, eligible and within 7d"
    assert counts["untriaged_expiring_30d"] == 3, "A, B, C — D/E/F are triaged, exempt or recalled"
    assert counts["first_expiry_date"] is not None


def test_expiry_counts_are_zero_when_expiry_is_off(kb):
    _i, qid = _unverified("obs", "off")
    _age_created(qid, 25)
    counts = review_auto.curation_counts(0)  # kill switch
    assert counts["untriaged_expiring_7d"] == 0
    assert counts["untriaged_expiring_30d"] == 0
    assert counts["first_expiry_date"] is None


def test_net_daily_flow_of_the_eligible_pool(kb):
    # entered today: two freshly minted unverified_extraction rows
    _p, _qp = _unverified("obs", "P")
    _q, _qq = _unverified("obs", "Q")
    # left today via access: an old row whose item was just recalled
    rid, qr = _unverified("obs", "R")
    _age_created(qr, 5)
    _record_access(rid)
    # left today via triage: an old row triaged just now
    _s, qs = _unverified("obs", "S")
    _age_created(qs, 5)
    review_triage.store_triage(qs, {"action": "keep", "dup_of": None, "rationale": "r"})
    # neither: an old row triaged five days ago (no recent triage, no access)
    _t, qt = _unverified("obs", "T")
    _age_created(qt, 5)
    review_triage.store_triage(qt, {"action": "keep", "dup_of": None, "rationale": "r"})
    _age_triage(qt, 5)

    counts = review_auto.curation_counts(30)
    assert counts["eligible_entered_1d"] == 2, "P and Q were minted in the last day"
    assert counts["eligible_left_1d"] == 2, "R (recalled) and S (triaged) left in the last day"
    assert counts["eligible_flow_net_1d"] == 0


# --- the digest line ----------------------------------------------------------


def test_digest_emits_the_expiry_line_when_the_7d_count_is_nonzero():
    lines = tail_steps.digest_lines({"queued_drained": 1, "untriaged_expiring_7d": 4})
    assert "drained: 1" in lines
    assert "EXPIRY: 4 untriaged rows expire within 7d" in lines


def test_digest_is_silent_when_the_7d_count_is_zero_or_absent():
    for counts in ({"queued_drained": 1, "untriaged_expiring_7d": 0}, {"queued_drained": 1}):
        assert not any(line.startswith("EXPIRY:") for line in tail_steps.digest_lines(counts))
