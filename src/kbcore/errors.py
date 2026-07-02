"""kbcore.errors — domain exceptions (CLAUDE.md §Code style).

kbcore raises these; the MCP layer converts them to clean tool errors and the
CLI to exit messages. Never let raw psycopg errors cross the service boundary.
"""

from __future__ import annotations


class KBError(Exception):
    """Base class for all Knowledge Vault domain errors."""


class NotFound(KBError):
    """Referenced item/entity does not exist."""


class DuplicateContent(KBError):
    """Ingest was a content-hash duplicate and no new item was created."""


class InvalidOperation(KBError):
    """Operation violates an invariant (e.g. superseding an already-superseded item)."""


class EmbedderUnavailable(KBError):
    """The Ollama embedding endpoint could not be reached or returned an error."""


class ExtractorUnavailable(KBError):
    """The Ollama extractor (/api/chat) could not be reached or returned unusable output."""
