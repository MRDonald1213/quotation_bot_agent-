"""In-process job registry so long LLM work does not freeze the browser.

Local single-user scale only: jobs live in memory and run one at a time, because one
person reviews one machine here, and a second concurrent SQLite writer would only
queue behind the first anyway.

Every job has a bounded lifetime. A run that never returns — a stalled local model call,
for instance — is closed by the registry once its deadline passes, so the document it was
started for can be processed again instead of staying locked forever.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..domain.exceptions import FreightAgentError, ValidationFailureError

logger = logging.getLogger("freight_agent.jobs")

DEFAULT_MAX_SECONDS = 300.0
MAX_JOBS_KEPT = 200


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _brief(exc: Exception) -> str:
    """One readable sentence for the page — never a SQL statement or document text."""
    if isinstance(exc, FreightAgentError):
        return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
    if "database is locked" in str(exc).casefold():
        return ("The local database was busy with another change, so this run stopped early. "
                "Nothing was lost — run this document again in a moment.")
    if "Timeout" in type(exc).__name__:
        return ("The local model did not answer within its time limit, so this run stopped early. "
                "Nothing was lost — process this document again, or raise LLM_TIMEOUT_SECONDS in .env "
                "if this model needs longer.")
    return (f"{type(exc).__name__}: this run stopped before finishing. "
            "The full reason is in the terminal where `freight review` is running.")


_EXPIRED = ("This run stopped responding, so it was closed after its time limit. "
            "Nothing was lost — process this document again.")


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
    max_seconds: float = DEFAULT_MAX_SECONDS
    gate_acquired: threading.Event = field(default_factory=threading.Event, repr=False)
    gate_released: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def active(self) -> bool:
        return self.status in {"queued", "running"}

    def deadline_at(self) -> datetime:
        return (self.started_at or self.created_at) + timedelta(seconds=max(1.0, self.max_seconds))

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
        self._sequence = 0

    def _release_gate_locked(self, job: Job) -> None:
        """Exactly one release per acquire, even when the registry closes a stalled run."""
        if job.gate_acquired.is_set() and not job.gate_released.is_set():
            job.gate_released.set()
            self._gate.release()

    def _expire_stale_locked(self, now: datetime) -> None:
        for job in self._jobs.values():
            if not job.active or job.deadline_at() > now:
                continue
            job.status = "failed"
            job.error = _EXPIRED
            job.progress = "stopped after its time limit"
            job.finished_at = now
            logger.warning("job %s (%s) exceeded %.0fs and was closed", job.id, job.kind, job.max_seconds)
            self._release_gate_locked(job)

    def _prune_locked(self) -> None:
        finished = [j for j in self._jobs.values() if not j.active]
        excess = len(finished) - MAX_JOBS_KEPT
        if excess <= 0:
            return
        for job in sorted(finished, key=lambda j: j.created_at)[:excess]:
            self._jobs.pop(job.id, None)

    def submit(self, kind: str, subject_id: str, work: Callable[[Job], Any],
               max_seconds: float = DEFAULT_MAX_SECONDS) -> Job:
        now = _now()
        with self._lock:
            self._expire_stale_locked(now)
            busy = [j for j in self._jobs.values() if j.subject_id == subject_id and j.active]
            if busy:
                remaining = max(0, int((busy[0].deadline_at() - now).total_seconds()))
                raise ValidationFailureError(
                    f"this document is still being processed (job {busy[0].id}); "
                    f"it closes on its own in about {remaining} seconds")
            self._sequence += 1
            job = Job(id=f"job-{self._sequence:04d}", kind=kind, subject_id=subject_id,
                      created_at=now, max_seconds=max_seconds)
            self._jobs[job.id] = job
            self._prune_locked()

        def run() -> None:
            self._gate.acquire()
            job.gate_acquired.set()
            try:
                with self._lock:
                    if not job.active:  # closed as stalled while this thread waited for the gate
                        return
                    job.status = "running"
                    job.started_at = _now()
                    job.progress = "running"
                result = work(job)
                with self._lock:
                    if not job.active:
                        logger.warning("job %s finished after it was already closed; result dropped", job.id)
                        return
                    job.status, job.result, job.progress = "succeeded", result, "done"
            except Exception as exc:  # noqa: BLE001 — the failure must be visible, but only in readable form
                logger.exception("job %s (%s) failed", job.id, job.kind)
                with self._lock:
                    if not job.active:
                        return
                    job.status = "failed"
                    job.error = _brief(exc)
                    job.progress = "stopped"
            finally:
                with self._lock:
                    if job.finished_at is None:
                        job.finished_at = _now()
                    self._release_gate_locked(job)

        threading.Thread(target=run, name=f"freight-{kind}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            self._expire_stale_locked(_now())
            job = self._jobs.get(job_id)
        if job is None:
            raise ValidationFailureError(f"unknown job: {job_id}")
        return job

    def latest_for(self, subject_id: str) -> Job | None:
        """The newest run for one subject, live or finished."""
        with self._lock:
            self._expire_stale_locked(_now())
            jobs = [j for j in self._jobs.values() if j.subject_id == subject_id]
        return max(jobs, key=lambda j: j.created_at) if jobs else None

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            self._expire_stale_locked(_now())
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]
        return [j.as_dict() for j in jobs]


__all__ = ["DEFAULT_MAX_SECONDS", "Job", "JobRunner"]
