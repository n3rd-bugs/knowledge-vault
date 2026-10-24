"""kbcore.unicode_safe — lone-surrogate sanitiser for text bound for a strict UTF-8 encoder.

Incident 2026-10-18 (repeater session 0854af52, 26 rows terminal-failed): an
extractor's JSON reply carried a ``"\\ud834"`` escape with no low half.
``json.loads`` accepts it and yields a Python str holding a LONE SURROGATE, and
every strict UTF-8 encoder downstream raises ``UnicodeEncodeError: ... surrogates
not allowed`` — httpx's ``json=`` (the ensemble's clustering embed, the judge's
prompt), psycopg (items, chunks, ``meta.quote``). The pinned extractor seed made
the reply byte-identical on every attempt, so the whole group burned its cap.

In a Python str a VALID astral character is ONE code point (``json.loads``
already joins an escaped high+low pair into U+1F916), so ANY code point in
U+D800..U+DFFF is unencodable by definition: :func:`clean` replaces each with
U+FFFD, the replacement character every ``errors="replace"`` decode here already
uses. Applied at the boundaries where such text enters: the model-JSON parse
(:func:`kbcore.llm_json.parse_model_json` and its salvage), transcript assembly
(:func:`kbworker.sessions.assemble_transcript` — a Node.js truncation can split a
pair, and ``JSON.stringify`` escapes the orphan), and defensively the embed
request (:func:`kbcore.embed.embed_texts`). Cheap on the hot path: one
precompiled search per string, and the SAME object comes back when nothing
matched.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

REPLACEMENT = "\ufffd"  # U+FFFD, what every errors="replace" decode here emits
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_CONTAINERS = (dict, list, tuple)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def clean(s: str) -> str:
    """``s`` with every surrogate code point replaced by U+FFFD; ``s`` itself
    (the same object) when it holds none."""
    if _SURROGATE_RE.search(s) is None:
        return s
    return _SURROGATE_RE.sub(REPLACEMENT, s)


def _cleaned(value: Any, done: dict[int, Any]) -> Any:
    """One child's cleaned form: a str is cleaned, a container is looked up in
    ``done`` (already rebuilt, children first), anything else passes through."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, _CONTAINERS):
        return done.get(id(value), value)
    return value


def _rebuilt(node: Any, done: dict[int, Any]) -> Any:
    """``node`` with its children replaced by their cleaned forms — ``node``
    itself when no child changed (identity is how a clean subtree reports)."""
    if isinstance(node, dict):
        pairs = [(_cleaned(k, done), _cleaned(v, done)) for k, v in node.items()]
        if all(k2 is k and v2 is v for (k2, v2), (k, v) in zip(pairs, node.items(), strict=True)):
            return node
        return dict(pairs)
    vals = [_cleaned(v, done) for v in node]
    if all(a is b for a, b in zip(vals, node, strict=True)):
        return node
    return vals if isinstance(node, list) else tuple(vals)


def clean_obj(x: Any) -> Any:
    """:func:`clean` applied to every str (dict keys included) inside dicts,
    lists and tuples, recursively; every other type passes through untouched.

    Returns the SAME object when nothing needed cleaning (also per subtree); a
    container that changed comes back as a plain dict/list/tuple copy (two keys
    that differ only in surrogates collapse, the later value winning). ITERATIVE
    on purpose: ``json.loads`` (3.12+) accepts nesting thousands deep, which a
    recursive walk would turn into a ``RecursionError`` the parse callers do not
    catch. A reference cycle (never produced by JSON) terminates, its back-edge
    left as is.
    """
    if isinstance(x, str):
        return clean(x)
    if not isinstance(x, _CONTAINERS):
        return x
    done: dict[int, Any] = {}
    stack: list[tuple[Any, bool]] = [(x, False)]
    while stack:
        node, expanded = stack.pop()
        if expanded:
            done[id(node)] = _rebuilt(node, done)
            continue
        if id(node) in done:
            continue  # shared subtree, already rebuilt (or an in-progress cycle)
        done[id(node)] = node  # provisional, so a cycle cannot loop forever
        stack.append((node, True))
        children = [*node.keys(), *node.values()] if isinstance(node, dict) else node
        stack.extend((c, False) for c in children if isinstance(c, _CONTAINERS))
    return done[id(x)]


def clean_result(fn: Callable[_P, _R]) -> Callable[_P, _R]:
    """Decorator: every value ``fn`` returns passes through :func:`clean_obj` —
    one guarantee for all of a parser's return paths, present and future."""

    @functools.wraps(fn)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return clean_obj(fn(*args, **kwargs))

    return wrapper
