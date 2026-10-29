"""kbcore.eval_echo — the golden ECHO RULE (E2: review #9 KB 40402, landed at review #10).

THE ESTABLISHED SEAM (mechanical split, review #10 fix round): the rule moved
here from :mod:`kbcore.eval_criteria` when that file reached its 300-line
ratchet. ``eval_criteria`` re-exports every name — ``ECHO_MIN_WORDS``,
``_norm``, ``echo_guarded``, ``is_echo`` — so the harness, the composition
rider (``kbworker.eval_composition``) and the suite keep resolving
``eval_criteria.is_echo``: ONE helper serves the harness's substring scan, the
composition's ``_matching_hits`` and the fused-rank telemetry, so the three can
never disagree on what an echo is.

E1's "clean" nights were a golden echo — an item quoting a golden query
verbatim plus its expected substring satisfied the case it described. A
GUARDED query is prose: >= :data:`ECHO_MIN_WORDS` ``\\w+`` words and NOT
identifier-dominant per search's own identifier leg (the MAX_ID_TOKENS-capped
``search_legs._id_dominant`` call ``kbcore.search`` makes — no second
threshold). A haystack ECHOES a guarded query when the query's normal form
(lowercased, whitespace runs collapsed, stripped) appears in the haystack's
normal form on WORD BOUNDARIES (review #10 fix F11): a phrase that only sits
inside longer words at either end ("show ... images" for "how ... image") is a
legitimate source, not a copy.

:func:`content_triage` is the CONTENT-ABSENT triage under the rule (fix F12):
a row that holds the expected substring only because it echoes the query is
not content, so a miss whose only holders are carriers reads CONTENT-ABSENT
"(only echo carriers hold it)" instead of a retrieval miss.
"""

from __future__ import annotations

import re

from kbcore.db import connect
from kbcore.search_legs import MAX_ID_TOKENS, _id_dominant, _identifier_tokens

ECHO_MIN_WORDS = 4  # a guarded query has at least this many \w+ words (review #9)
# Rows read per expected substring before the triage gives up proving that ONLY
# echo carriers hold it (a capped read can never prove a negative: 'present').
CONTENT_SCAN_CAP = 20
_WS_RUN = re.compile(r"\s+")
_LIKE_META = re.compile(r"[!%_]")  # escaped with '!' (ESCAPE '!'), so '\' is plain text too


def like_pattern(sub: str) -> str:
    """``%sub%`` for ``ILIKE ... ESCAPE '!'`` — the substring LITERALLY (fix-round H2).

    Raw, ``_`` / ``%`` were wildcards (a 'quokka-gear' row "held" 'quokka_gear')
    and ``\\`` LIKE's default escape (a literal 'C:\\kb' holder went unfound).
    """
    return "%" + _LIKE_META.sub(lambda m: "!" + m.group(0), sub) + "%"


def _norm(text: str) -> str:
    """The echo normal form: lowercased, every whitespace run one space, stripped."""
    return _WS_RUN.sub(" ", text.lower()).strip()


def echo_guarded(query: str) -> bool:
    """Is ``query`` prose enough that a verbatim copy of it is an ECHO? (module docstring)

    EXACTLY ``kbcore.search``'s own identifier-dominance call — the
    MAX_ID_TOKENS-capped token list into ``search_legs._id_dominant`` — so the
    rule can never disagree with the leg that decides what an identifier
    query is. No second threshold.
    """
    if len(re.findall(r"\w+", query)) < ECHO_MIN_WORDS:
        return False
    return not _id_dominant(query, _identifier_tokens(query)[:MAX_ID_TOKENS])


def is_echo(query: object, haystack: str) -> bool:
    """True when a guarded ``query`` appears verbatim (normalised, on word boundaries)
    inside ``haystack``."""
    if not (isinstance(query, str) and echo_guarded(query)):
        return False
    pattern = r"(?<!\w)" + re.escape(_norm(query)) + r"(?!\w)"
    return re.search(pattern, _norm(haystack)) is not None


def content_triage(expected: list[str], query: str) -> str:
    """Who holds an ``expect_substr`` miss's substrings: 'present' | 'echo-only' | 'absent'.

    'present' — some corpus row (any status) holds one and is NOT an echo
    carrier of ``query`` (a retrieval miss); 'echo-only' — rows hold them but
    every one is a carrier; 'absent' — no row holds any (a corpus problem).
    A row HOLDS a substring literally (:func:`like_pattern`, then the harness's
    own case-folded ``in``), never by LIKE wildcard. Bounded: at most
    :data:`CONTENT_SCAN_CAP` rows per substring are read, and a capped read
    that saw only carriers reads 'present' (it cannot prove that no other row
    exists). Parameterized, read-only.
    """
    found = capped = False
    with connect() as conn:
        for sub in expected:
            pattern = like_pattern(sub)
            rows = conn.execute(
                "SELECT title, body FROM items WHERE title ILIKE %s ESCAPE '!' "
                "OR body ILIKE %s ESCAPE '!' LIMIT %s",
                (pattern, pattern, CONTENT_SCAN_CAP),
            ).fetchall()
            texts = [f"{title}\n{body or ''}" for title, body in rows]
            holders = [t for t in texts if sub.lower() in t.lower()]
            if any(not is_echo(query, text) for text in holders):
                return "present"
            found = found or bool(holders)
            capped = capped or len(rows) >= CONTENT_SCAN_CAP
    if capped:
        return "present"
    return "echo-only" if found else "absent"
