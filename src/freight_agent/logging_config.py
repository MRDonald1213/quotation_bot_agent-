from __future__ import annotations

import logging
import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class EmailMaskingFilter(logging.Filter):
    """Never log sensitive content: email addresses are masked even with --verbose."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        masked = _EMAIL_RE.sub(lambda m: f"***@{m.group(0).split('@')[-1]}", msg)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


def setup_logging(verbose: bool = False) -> None:
    level = logging.INFO if verbose else logging.WARNING
    handler = logging.StreamHandler()
    handler.addFilter(EmailMaskingFilter())
    logging.basicConfig(level=level, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
