"""In-process job registry so long LLM work does not freeze the browser.

Local single-user scale only: jobs live in memory and run one at a time, because one
person reviews one machine here, and a second concurrent SQLite writer would only
queue behind the first anyway.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..domain.exceptions import FreightAgentError, ValidationFailureError

logger = logging.getLogger("freight_agent.jobs")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _brief(exc: Exception) -> str:
    """One readable sentence for the page — never a SQL statement or document text."""
    if isinstance(exc, FreightAgentError):
        return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
    if "database is locked" in str(exc).casefold():
        return ("The local database was busy with another change, so this run stopped early. "
                "Nothing was lost — run this document again in a moment.")
    return (f"{type(exc).__name__}: this run stopped before finishing. "
            "The full reason is in the terminal where `freight review` is running.")


@dataclass
class Job:
    id: str
    kind: str
    subject_id: str
    status: str = "queued"
    progress: str = "waiting for a free worker"
    result: Any = None
    error: str | None = None
    created_at: datetime = field(default_factory=_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "subject_id": self.subject_id, "status": self.status,
                "progress": self.progress, "result": self.result, "error": self.error,
                "created_at": self.created_at.isoformat(),
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None}


class JobRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._gate = threading.Semaphore(1)

    def submit(self, kind: str, subject_id: str, work: Callable[[Job], Any]) -> Job:
        with self._lock:
            running = [j for j in self._jobs.values()
                       if j.subject_id == subject_id and j.status in {"queued", "running"}]
            if running:
                raise ValidationFailureError(
                    f"this document is already being processed (job {running[0].id}); wait for it to finish")
            job = Job(id=f"job-{len(self._jobs) + 1:04d}", kind=kind, subject_id=subject_id)
            self._jobs[job.id] = job

        def run() -> None:
            self._gate.acquire()
            try:
                with self._lock:
                    job.status = "running"
                    job.started_at = _now()
                    job.progress = "running"
                result = work(job)
                with self._lock:
                    job.status, job.result, job.progress = "succeeded", result, "done"
            except Exception as exc:  # noqa: BLE001 — the failure must be visible, but only in readable form
                logger.exception("job %s (%s) failed", job.id, job.kind)
                with self._lock:
                    job.status = "failed"
                    job.error = _brief(exc)
                    job.progress = "stopped"
            finally:
                with self._lock:
                    job.finished_at = _now()
                self._gate.release()

        threading.Thread(target=run, name=f"freight-{kind}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise ValidationFailureError(f"unknown job: {job_id}")
        return job

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]
        return [j.as_dict() for j in jobs]


__all__ = ["Job", "JobRunner"]
