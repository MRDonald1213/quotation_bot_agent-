from __future__ import annotations


class FreightAgentError(Exception):
    """Base for all typed application errors."""

    exit_code = 1


class ConfigurationError(FreightAgentError):
    exit_code = 2


class ModelUnavailableError(FreightAgentError):
    """Ollama unreachable / model missing. Never falls back to a cloud model."""

    exit_code = 3


class LLMOutputInvalidError(FreightAgentError):
    """Structured LLM output failed schema or domain validation after bounded retries."""

    exit_code = 4


class DocumentParsingError(FreightAgentError):
    exit_code = 5


class DuplicateIntakeError(FreightAgentError):
    exit_code = 6

    def __init__(self, sha256: str, existing_intake_id: str, filename: str) -> None:
        self.sha256 = sha256
        self.existing_intake_id = existing_intake_id
        self.filename = filename
        super().__init__(
            f"Duplicate content (sha256={sha256[:16]}…) already imported as intake "
            f"{existing_intake_id} ({filename}). Re-run with --on-duplicate skip|reprocess."
        )


class ValidationFailureError(FreightAgentError):
    exit_code = 7


class NotFoundError(FreightAgentError):
    exit_code = 8


class DatabaseError(FreightAgentError):
    exit_code = 9


class ReplyAssociationRequiredError(ValidationFailureError):
    def __init__(self, candidates: list) -> None:
        self.candidates = candidates
        super().__init__(
            "reply association is uncertain; select a quotation with --quote "
            "(and --carrier if needed). No reply has been imported."
        )
