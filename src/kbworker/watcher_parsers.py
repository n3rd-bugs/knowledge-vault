"""kbworker.watcher_parsers — inbox file classification + text extraction (§7.1).

Split out of ``watcher.py`` to keep each module small (CLAUDE.md: files < 300
lines). PDF text via ``pypdf``, DOCX via ``python-docx``; source-code
extensions are routed through the code splitter as ``snippet``, prose as
``note``.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

# Source-code extensions → code splitter, type='snippet' (§7.1). These are
# curated snippets a human deliberately dropped, never a bulk source tree
# (Invariant 9 lives in reposcan; the inbox is human-gated).
SOURCE_EXTS = frozenset({".py", ".c", ".h", ".cpp", ".ino", ".js", ".ts", ".json", ".yaml", ".sh"})
# Prose extensions → markdown chunking, type='note'.
NOTE_EXTS = frozenset({".md", ".txt"})
# Binary document extensions → text extraction, type='note'.
DOC_EXTS = frozenset({".pdf", ".docx"})
SUPPORTED_EXTS = SOURCE_EXTS | NOTE_EXTS | DOC_EXTS


@dataclass
class ParsedFile:
    """Extraction result for one inbox file."""

    raw: bytes  # original bytes, archived verbatim under KB_RAW (OPS-4)
    text: str  # extracted text that becomes the item body
    type: str  # kbcore item type ('snippet' | 'note')
    is_source: bool  # routes ingest() to the code splitter (§5.2)


def is_supported(path: Path) -> bool:
    """True if the extension is one the watcher knows how to ingest."""
    return path.suffix.lower() in SUPPORTED_EXTS


def _pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


def _docx_text(raw: bytes) -> str:
    from docx import Document

    doc = Document(io.BytesIO(raw))
    return "\n".join(p.text for p in doc.paragraphs).strip()


def parse(path: Path) -> ParsedFile:
    """Read and extract text from a supported inbox file.

    Raises ``ValueError`` for an unsupported extension so callers can skip it,
    and lets pypdf/python-docx errors propagate (a truncated/partial file — the
    watcher retries once it is stable).
    """
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"unsupported inbox file type {ext!r}: {path}")
    raw = path.read_bytes()
    if ext == ".pdf":
        return ParsedFile(raw=raw, text=_pdf_text(raw), type="note", is_source=False)
    if ext == ".docx":
        return ParsedFile(raw=raw, text=_docx_text(raw), type="note", is_source=False)
    text = raw.decode("utf-8", errors="replace")
    if ext in SOURCE_EXTS:
        return ParsedFile(raw=raw, text=text, type="snippet", is_source=True)
    return ParsedFile(raw=raw, text=text, type="note", is_source=False)
