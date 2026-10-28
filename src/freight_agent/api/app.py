"""Local FastAPI backend for the Freight AI Agent web app.

Every route is a thin wrapper around an existing service or repository call: the
workflows, validation, transactions and audit trail stay exactly where the CLI has
them. The server binds to 127.0.0.1 and the browser is never allowed to talk to
Ollama directly.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from ..config import Settings, is_local_endpoint
from ..domain.enums import ShipmentStatus
from ..domain.exceptions import (ConfigurationError, DatabaseError, DocumentParsingError,
                                 DuplicateIntakeError, FreightAgentError, ModelUnavailableError,
                                 NotFoundError, ValidationFailureError)
from ..parsers.base import SUPPORTED_EXTENSIONS
from ..services.shipments import route_label
from ..services.review import (apply_action, approve_review, build_review, import_reviewed,
                               preview_import, reject_review, set_evidence_status, shipment_label)
from .jobs import JobRunner

STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "static"
STATUS_CODES: dict[type, int] = {
    NotFoundError: 404,
    DuplicateIntakeError: 409,
    ModelUnavailableError: 503,
    ConfigurationError: 500,
    DatabaseError: 500,
    DocumentParsingError: 415,
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # transport ceiling; the configured parser limit is stricter


def _upload_limit(settings: Settings) -> int:
    return min(MAX_UPLOAD_BYTES, max(1, settings.max_file_size_mb) * 1024 * 1024)


def _document_rows(settings: Settings) -> list[dict[str, Any]]:
    """Every intake, newest first, with the labels a person reads rather than ids."""
    from ..services.common import with_session

    with with_session(settings) as repos:
        items = list(reversed(repos.list_intakes(None, None)))
        lanes = {str(s.id): s for s in repos.list_shipments()}
    rows = []
    for item in items:
        lane = lanes.get(str(item.shipment_id)) if item.shipment_id else None
        rows.append({
            "id": str(item.id), "file": item.original_filename, "source": str(item.source_type),
            "size_bytes": item.file_size_bytes, "sha256": item.content_sha256[:16],
            "classification": str(item.classification_status), "confidence": item.classification_confidence,
            "processing": str(item.processing_status), "shipment_id": str(item.shipment_id) if item.shipment_id else None,
            "shipment_label": shipment_label(lane) if lane is not None else None,
            "created_at": item.created_at.isoformat(), "reason": item.classification_reason,
            "chars": len(item.raw_text),
        })
    return rows


def _next_step(row: dict[str, Any]) -> str:
    if row["classification"] == "irrelevant":
        return "See why it was set aside"
    if row["classification"] == "needs_review":
        return "Check the decision"
    if row["classification"] == "failed" or row["processing"] == "failed":
        return "Read it again"
    if row["processing"] == "processed":
        return "Check the extracted figures"
    return "Read this document"


def _error(exc: Exception, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": str(exc),
                                                     "type": type(exc).__name__})


def create_app(settings: Settings) -> FastAPI:
    from ..db.engine import get_session
    from ..db.migrations import init_schema, schema_status

    app = FastAPI(title="Freight AI Agent", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.runner = JobRunner()
    with get_session(settings) as bootstrap_session:
        init_schema(bootstrap_session)

    @app.exception_handler(FreightAgentError)
    def _domain_errors(request: Request, exc: FreightAgentError) -> JSONResponse:  # noqa: ARG001
        return _error(exc, STATUS_CODES.get(type(exc), 400))

    @app.exception_handler(RequestValidationError)
    def _request_errors(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=422, content={"ok": False,
                                                      "error": "invalid request: " + str(exc.errors()[:3])})

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method != "GET":
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            if origin and not any(host in origin for host in ("127.0.0.1", "localhost", "[::1]")):
                return JSONResponse(status_code=403, content={"ok": False, "error": "cross-origin request refused"})
        return await call_next(request)

    def runner() -> JobRunner:
        return app.state.runner

    # ------------------------------------------------------------------ status
    @app.get("/api/status")
    def status() -> dict[str, Any]:
        from ..agents.model import check_ollama

        session = get_session(settings)
        try:
            session.execute(text("SELECT 1"))
            initialized, version = schema_status(session)
        finally:
            session.close()
        if not is_local_endpoint(settings.ollama_base_url):
            ollama = {"reachable": False, "detail": "configured endpoint is not local; remote models are refused"}
            models: list[str] = []
        else:
            reachable, detail, models = check_ollama(settings)
            ollama = {"reachable": reachable, "detail": detail}
        parsers = {}
        for name, module in (("pdf", "pypdf"), ("docx", "docx"), ("xlsx", "openpyxl")):
            try:
                __import__(module)
                parsers[name] = True
            except ImportError:
                parsers[name] = False
        return {
            "backend": "ok",
            "database": {"url": settings.database_url, "path": str(settings.database_path),
                         "initialized": initialized, "schema_version": version},
            "ollama": {**ollama, "model": settings.ollama_model, "base_url": settings.ollama_base_url,
                       "model_installed": settings.ollama_model in models if models else None,
                       "available_models": models},
            "workspace": str(settings.workspace_dir),
            "limits": {"max_file_size_mb": settings.max_file_size_mb,
                       "confidence_threshold": settings.classification_confidence_threshold,
                       "supported_extensions": sorted(SUPPORTED_EXTENSIONS)},
            "parsers": parsers,
        }

    # --------------------------------------------------------------- dashboard
    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        review = build_review(settings)
        rows = _document_rows(settings)
        recent = [{**row, "next_action": _next_step(row), "target": f"#/documents/{row['id']}"}
                  for row in rows[:8]]
        return {"counts": {**review["counts"], "all_documents": len(rows)}, "recent": recent,
                "proposals": [s["label"] for s in review["shipments"]
                              if s["status"] == str(ShipmentStatus.needs_review)],
                "activity": review["activity"][:6], "jobs": runner().recent(5)}

    @app.get("/api/review")
    def review_payload() -> dict[str, Any]:
        return build_review(settings)

    # ---------------------------------------------------------------- documents
    @app.get("/api/documents")
    def documents(status: str | None = None, kind: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        rows = _document_rows(settings)
        if status:
            rows = [r for r in rows if r["processing"] == status or r["classification"] == status]
        if kind:
            rows = [r for r in rows if r["file"].lower().endswith("." + kind.lower().lstrip("."))]
        if q:
            needle = q.lower()
            rows = [r for r in rows if needle in r["file"].lower() or needle in (r["reason"] or "").lower()]
        return rows

    @app.post("/api/documents", status_code=201)
    async def upload(request: Request) -> dict[str, Any]:
        from ..services import intake as intake_service

        form = await request.form()
        files = [value for key, value in form.multi_items() if key == "files" and hasattr(value, "read")]
        on_duplicate = str(form.get("on_duplicate") or "error")
        if not files:
            raise ValidationFailureError("no files received")
        limit = _upload_limit(settings)
        results = []
        for upload in files:
            payload = await upload.read(limit + 1)
            name = Path(upload.filename or "document").name or "document"
            entry: dict[str, Any] = {"file": name, "size_bytes": len(payload)}
            if len(payload) > limit:
                entry.update({"status": "rejected",
                              "error": f"larger than the configured limit of {limit // 1024 // 1024} MB"})
                results.append(entry)
                continue
            if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                entry.update({"status": "rejected",
                              "error": f"unsupported type {Path(name).suffix!r}; allowed: "
                                       f"{', '.join(sorted(SUPPORTED_EXTENSIONS))}"})
                results.append(entry)
                continue
            # the browser filename is kept so provenance and evidence cite the real document
            with tempfile.TemporaryDirectory(prefix="freight-upload-") as staging:
                temp_path = Path(staging) / name
                temp_path.write_bytes(payload)
                try:
                    item, skipped = intake_service.register_intake(settings, None, temp_path,
                                                                  on_duplicate=on_duplicate)
                    entry.update({"status": "duplicate" if skipped else "registered",
                                  "intake_id": str(item.id),
                                  "message": ("already imported as " + str(item.id))[:200] if skipped
                                  else "stored and parsed locally; run processing to extract"})
                except DuplicateIntakeError as exc:
                    entry.update({"status": "duplicate", "intake_id": exc.existing_intake_id,
                                  "error": str(exc), "options": ["skip", "reprocess"]})
                except FreightAgentError as exc:
                    entry.update({"status": "rejected", "error": str(exc)})
            results.append(entry)
        return {"ok": True, "results": results}

    @app.get("/api/documents/{intake_id}")
    def document_detail(intake_id: str) -> dict[str, Any]:
        from ..services.common import with_session

        with with_session(settings) as repos:
            item = repos.get_intake(intake_id)
            quotes = [q for q in repos.list_quotations(str(item.shipment_id) if item.shipment_id else None)
                      if str(q.intake_item_id) == str(item.id)]
            shipment = repos.get_shipment(str(item.shipment_id)) if item.shipment_id else None
            carriers = {c.id: c.name for c in repos.list_carriers()}
            payload = {
                "id": str(item.id), "file": item.original_filename, "source": str(item.source_type),
                "sender": item.sender, "subject": item.subject, "sha256": item.content_sha256,
                "size_bytes": item.file_size_bytes, "chars": len(item.raw_text),
                "classification": str(item.classification_status), "confidence": item.classification_confidence,
                "processing": str(item.processing_status), "reason": item.classification_reason,
                "created_at": item.created_at.isoformat(), "stored_path": item.stored_path,
                "text": item.raw_text[:20000], "text_truncated": len(item.raw_text) > 20000,
                "shipment": None if shipment is None else {
                    "id": str(shipment.id), "reference": shipment.reference, "status": str(shipment.status),
                    "label": shipment_label(shipment), "route": route_label(shipment.origin, shipment.destination),
                    "origin": shipment.origin, "destination": shipment.destination,
                    "updated_at": shipment.updated_at.isoformat(),
                    "route_evidence": json.loads(shipment.route_evidence_json or "[]"),
                    "route_category": shipment.route_category},
                "quotations": [],
            }
            for quote in quotes:
                evidence = repos.evidence_for_quotation(str(quote.id))
                gaps = [g for g in repos.list_gaps(str(quote.shipment_id))
                        if g.quotation_id is None or str(g.quotation_id) == str(quote.id)]
                dump = quote.model_dump(mode="json")
                dump["carrier"] = carriers.get(quote.carrier_id) or (quote.quote_reference or "carrier not named")
                payload["quotations"].append({
                    "quote": dump,
                    "evidence": [e.model_dump(mode="json") for e in evidence],
                    "gaps": [g.model_dump(mode="json") for g in gaps if str(g.status) == "open"],
                })
            return payload

    @app.get("/api/documents/{intake_id}/source")
    def document_source(intake_id: str) -> FileResponse:
        from ..services.common import with_session

        with with_session(settings) as repos:
            item = repos.get_intake(intake_id)
        path = Path(item.stored_path)
        if not path.is_file():
            raise NotFoundError(f"stored file is missing on disk: {item.original_filename}")
        return FileResponse(path, filename=item.original_filename)

    @app.post("/api/documents/{intake_id}/process")
    def process_document(intake_id: str, force: bool = False) -> dict[str, Any]:
        from ..services.extraction import extract_and_store_quote

        def work(job) -> dict[str, Any]:
            job.progress = "classifying, extracting and resolving the route…"
            summary = extract_and_store_quote(settings, intake_id, force=force)
            return summary.model_dump(mode="json")

        return {"ok": True, "job": runner().submit("process", intake_id, work).as_dict()}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        return runner().get(job_id).as_dict()

    @app.get("/api/jobs")
    def jobs_recent() -> list[dict[str, Any]]:
        return runner().recent(20)

    # ------------------------------------------------------------------ review
    @app.post("/api/quotes/{quotation_id}/review/approve")
    def quote_approve(quotation_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        quote = approve_review(settings, quotation_id, body.get("notes"))
        return {"ok": True, "review_status": str(quote.review_status), "quotation_id": str(quote.id),
                "message": "review approved — import is a separate step"}

    @app.post("/api/quotes/{quotation_id}/review/reject")
    def quote_reject(quotation_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        quote = reject_review(settings, quotation_id, body.get("reason"))
        return {"ok": True, "review_status": str(quote.review_status),
                "message": "marked not relevant; nothing was imported"}

    @app.get("/api/quotes/{quotation_id}/import/preview")
    def import_preview(quotation_id: str) -> dict[str, Any]:
        return preview_import(settings, quotation_id)

    @app.post("/api/quotes/{quotation_id}/import")
    def quote_import(quotation_id: str) -> dict[str, Any]:
        return {"ok": True, **import_reviewed(settings, quotation_id)}

    @app.post("/api/evidence/{evidence_id}/decide")
    def evidence_decide(evidence_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        evidence = set_evidence_status(settings, evidence_id, bool(body.get("accept")))
        return {"ok": True, "id": str(evidence.id), "validation_status": str(evidence.validation_status)}

    # --------------------------------------------------------------- shipments
    @app.get("/api/shipments")
    def shipments() -> list[dict[str, Any]]:
        review = build_review(settings)
        return [{"id": s["id"], "reference": s["reference"], "label": s["label"], "route": s["route"],
                 "origin": s["origin"],
                 "destination": s["destination"], "status": s["status"], "route_category": s["route_category"],
                 "target_currency": s["target_currency"], "quotes": len(s["quotes"]),
                 "open_gaps": s["open_gaps"], "drafts": len(s["drafts"])} for s in review["shipments"]]

    @app.get("/api/shipments/{shipment_id}")
    def shipment_detail(shipment_id: str) -> dict[str, Any]:
        review = build_review(settings)
        match = next((s for s in review["shipments"] if s["id"] == shipment_id), None)
        if match is None:
            raise NotFoundError(f"shipment not found: {shipment_id}")
        return match

    @app.get("/api/shipments/{shipment_id}/report")
    def shipment_report(shipment_id: str, format: str = Query("md", pattern="^(md|json|csv)$")) -> FileResponse:
        from ..services.comparison import export_report

        path = export_report(settings, shipment_id, fmt=format)
        return FileResponse(path, filename=Path(path).name, media_type="text/plain")

    # ------------------------------------------------------------------ drafts
    @app.get("/api/drafts")
    def drafts() -> list[dict[str, Any]]:
        review = build_review(settings)
        return [{**d, "shipment_id": s["id"], "shipment_reference": s["reference"]}
                for s in review["shipments"] for d in s["drafts"]]

    @app.post("/api/drafts/{draft_id}/approve")
    def draft_approve(draft_id: str) -> dict[str, Any]:
        from ..services.clarification import approve_draft

        draft = approve_draft(settings, draft_id)
        return {"ok": True, "status": str(draft.status), "message": f"approved: {draft.subject}"}

    @app.post("/api/drafts/{draft_id}/export")
    def draft_export(draft_id: str, body: dict[str, Any] = Body(default={})) -> Response:
        from ..services.clarification import export_draft

        fmt = str(body.get("format") or "eml")
        path = export_draft(settings, draft_id, format=fmt, recipient=body.get("recipient") or None)
        data = Path(path).read_bytes()
        return Response(content=data, media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{Path(path).name}"'})

    # ------------------------------------------------------------- generic verbs
    @app.post("/api/action")
    def action(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return {"ok": True, **apply_action(settings, str(body.get("action") or ""), body)}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


__all__ = ["JobRunner", "create_app"]
