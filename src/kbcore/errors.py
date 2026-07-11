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
    """The Ollama extractor (/api/chat) could not be reached (connectivity blip).

    Reserved for TRANSIENT availability failures — connect errors, a model that is
    not pulled/loaded, a 5xx server hiccup. The queue drain DEFERS these (row stays
    'pending', attempts unchanged) so the next run retries once Ollama is back.
    """


class ExtractionFailed(KBError):
    """The extractor ran but the FAILURE is deterministic in this row's content.

    Unparseable / unterminated-<think> output, an HTTP 4xx shaped by the request,
    or a per-row read timeout on the long budget — re-running the 14B on the same
    text will fail identically. The queue drain marks these 'failed' (incrementing
    attempts) so the attempts cap bounds a poison row instead of retrying forever.
    """
