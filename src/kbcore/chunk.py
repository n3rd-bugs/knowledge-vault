"""kbcore.chunk — structure-aware chunker (DATA-4, PLAN.md §5.2).

Prose/markdown: split on headings; code fences and tables are atomic (never
split, oversize permitted); pack paragraphs to 300–500 tokens with ~15%
overlap. Every chunk is prepended with a breadcrumb
``"{title} › {heading path}\n\n"``.

Raw source files (curated snippets only, Invariant 9): heuristic top-level-
definition splitter, then pack to size. Tree-sitter deferred (verdict V8).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import tiktoken

MIN_TOKENS = 300
MAX_TOKENS = 500
OVERLAP_TOKENS = 75  # ~15% of MAX_TOKENS

_enc = tiktoken.get_encoding("cl100k_base")

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

# Top-level definition starts for the heuristic code splitter (§5.2): python,
# C-ish function definitions, JS/TS declarations.
_DEF_LINE = re.compile(
    r"^def |^class |^(static |inline )?\w[\w\s\*]+\([^;]*\)\s*\{|^function |"
    r"^(export )?(const|class|function) "
)


def count_tokens(text: str) -> int:
    return len(_enc.encode(text, disallowed_special=()))


@dataclass
class _Block:
    text: str
    heading_path: tuple[str, ...]
    atomic: bool  # code fence or table: never split, never trimmed for overlap


def _parse_markdown_blocks(body: str) -> list[_Block]:
    """Split markdown into paragraph/fence/table blocks tagged with heading path."""
    blocks: list[_Block] = []
    path: list[tuple[int, str]] = []  # (level, text)
    para: list[str] = []
    fence: list[str] = []
    fence_close: re.Pattern[str] | None = None
    table: list[str] = []

    def heading_path() -> tuple[str, ...]:
        return tuple(t for _, t in path)

    def flush_para() -> None:
        if para:
            blocks.append(_Block("\n".join(para).strip(), heading_path(), atomic=False))
            para.clear()

    def flush_table() -> None:
        if table:
            blocks.append(_Block("\n".join(table), heading_path(), atomic=True))
            table.clear()

    for line in body.split("\n"):
        if fence_close is not None:
            fence.append(line)
            # CommonMark close: same char, at least as many, nothing but
            # whitespace after — an inner "```python" open never closes an
            # outer ```` fence (review finding: startswith() misfired here).
            if fence_close.match(line.strip()):
                blocks.append(_Block("\n".join(fence), heading_path(), atomic=True))
                fence, fence_close = [], None
            continue
        m = _FENCE_OPEN.match(line.strip())
        if m:
            flush_para()
            flush_table()
            marker = m.group(1)
            fence_close = re.compile(rf"^{re.escape(marker[0])}{{{len(marker)},}}\s*$")
            fence = [line]
            continue
        if _TABLE_ROW.match(line):
            flush_para()
            table.append(line)
            continue
        flush_table()
        h = _HEADING.match(line)
        if h:
            flush_para()
            level, text = len(h.group(1)), h.group(2).strip()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, text))
            continue
        if not line.strip():
            flush_para()
        else:
            para.append(line)
    if fence:  # unterminated fence: keep what we have, atomic
        blocks.append(_Block("\n".join(fence), heading_path(), atomic=True))
    flush_para()
    flush_table()
    return [b for b in blocks if b.text.strip()]


def _breadcrumb(title: str, heading_path: tuple[str, ...]) -> str:
    return " › ".join((title, *heading_path)) + "\n\n"


def _split_oversize(block: _Block) -> list[_Block]:
    """Token-split a non-atomic block that alone exceeds MAX_TOKENS.

    Balanced parts (each ≤ MAX_TOKENS, ≥ ~MIN_TOKENS for large inputs) so a
    wall-of-text paragraph can't become one unbounded chunk (review finding).
    Atomic blocks are returned untouched — oversize is permitted for them.
    """
    tokens = _enc.encode(block.text, disallowed_special=())
    if block.atomic or len(tokens) <= MAX_TOKENS:
        return [block]
    parts = -(-len(tokens) // MAX_TOKENS)  # ceil
    size = -(-len(tokens) // parts)
    return [
        _Block(_enc.decode(tokens[i : i + size]), block.heading_path, atomic=False)
        for i in range(0, len(tokens), size)
    ]


def _pack(blocks: list[_Block], title: str) -> list[str]:
    """Pack blocks into chunks of MIN..MAX tokens with ~15% overlap.

    Heading changes are hard boundaries (split on headings first); atomic
    blocks are emitted whole even when oversize.
    """
    chunks: list[str] = []
    cur: list[_Block] = []
    cur_tokens = 0
    cur_path: tuple[str, ...] | None = None
    overlap_count = 0  # leading blocks of `cur` that are pure overlap carry-over

    def emit() -> None:
        nonlocal cur, cur_tokens, overlap_count
        # A trailing chunk made only of overlap blocks duplicates content the
        # previous chunk already carries — skip it (review finding).
        if cur and not (chunks and overlap_count == len(cur)):
            crumb = _breadcrumb(title, cur[0].heading_path)
            chunks.append(crumb + "\n\n".join(b.text for b in cur))
        cur, cur_tokens, overlap_count = [], 0, 0

    def overlap_tail(prev: list[_Block]) -> list[_Block]:
        """Trailing non-atomic blocks of the previous chunk, up to OVERLAP_TOKENS."""
        tail: list[_Block] = []
        total = 0
        for b in reversed(prev):
            if b.atomic:
                break
            t = count_tokens(b.text)
            if total + t > OVERLAP_TOKENS:
                break
            tail.insert(0, b)
            total += t
        return tail

    for block in (piece for b in blocks for piece in _split_oversize(b)):
        n = count_tokens(block.text)
        if cur_path is not None and block.heading_path != cur_path:
            emit()  # heading boundary: no overlap across sections
        elif cur and cur_tokens + n > MAX_TOKENS:
            prev = cur.copy()
            emit()
            for t in overlap_tail(prev):
                cur.append(t)
                cur_tokens += count_tokens(t.text)
            overlap_count = len(cur)
        cur_path = block.heading_path
        cur.append(block)
        cur_tokens += n
        if block.atomic and n > MAX_TOKENS:
            emit()  # oversize atomic block closes its chunk immediately
    emit()
    return chunks


def chunk_markdown(title: str, body: str) -> list[str]:
    """Chunk prose/markdown per §5.2. Returns breadcrumbed chunk texts."""
    blocks = _parse_markdown_blocks(body)
    if not blocks:
        return []
    return _pack(blocks, title)


def chunk_source(title: str, body: str) -> list[str]:
    """Chunk a raw source file by top-level definitions, packing to size.

    Each definition is atomic (a huge function becomes one oversize chunk
    rather than being split mid-body).
    """
    lines = body.split("\n")
    starts = [i for i, line in enumerate(lines) if _DEF_LINE.match(line)]
    if not starts:
        pieces = [body]
    else:
        bounds = ([0] if starts[0] != 0 else []) + starts + [len(lines)]
        pieces = ["\n".join(lines[a:b]).strip() for a, b in zip(bounds, bounds[1:], strict=False)]
    blocks = [_Block(p, (), atomic=True) for p in pieces if p.strip()]
    return _pack(blocks, title)


def chunk_text(title: str, body: str, *, is_source: bool = False) -> list[str]:
    """Entry point used by ingest: dispatch to the markdown or source chunker."""
    if is_source:
        return chunk_source(title, body)
    return chunk_markdown(title, body)
