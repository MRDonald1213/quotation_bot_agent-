"""Local FastAPI backend for the Freight AI Agent web app.

Every route is a thin wrapper around an existing service or repository call: the
workflows, validation, transactions and audit trail stay exactly where the CLI has
them. The server binds to 127.0.0.1 and the browser is never allowed to talk to
Ollama directly.
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from secrets import token_urlsafe
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from ..config import Settings, is_local_endpoint
from ..domain.enums import ShipmentStatus
from ..domain.models import WorkspaceAction, ComparisonQuery
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
    from ..services.quotation_naming import name_for_intake

    with with_session(settings) as repos:
        items = list(reversed(repos.list_intakes(None, None)))
        lanes = {str(s.id): s for s in repos.list_shipments()}
        names = {str(item.id): name_for_intake(repos.session, str(item.id)) for item in items}
    rows = []
    for item in items:
        lane = lanes.get(str(item.shipment_id)) if item.shipment_id else None
        rows.append({
            "id": str(item.id), "name": names.get(str(item.id), "Quotation"),
            "file": item.original_filename, "source": str(item.source_type),
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


def _processing_budget_seconds(settings: Settings) -> float:
    """Allow one bounded extraction for each active destination plus overhead."""
    from ..db.engine import get_session
    from ..db.models import SupplierDatabaseRow
    with get_session(settings) as session:
        count = session.query(SupplierDatabaseRow).filter_by(status="active").count()
    return settings.llm_timeout_seconds * (settings.llm_max_retries + 1) * max(2, count) + 120


def _error(exc: Exception, status: int) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": str(exc),
                                                     "type": type(exc).__name__})


def create_app(settings: Settings) -> FastAPI:
    from ..db.engine import get_session
    from ..db.migrations import init_schema, schema_status

    app = FastAPI(title="Freight AI Agent", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.sessions = {}
    app.state.settings = settings
    app.state.runner = JobRunner()
    app.state.oauth_pending_states: dict[str, tuple[float, str]] = {}
    with get_session(settings) as bootstrap_session:
        init_schema(bootstrap_session)

    @app.exception_handler(FreightAgentError)
    def _domain_errors(request: Request, exc: FreightAgentError) -> JSONResponse:  # noqa: ARG001
        return _error(exc, STATUS_CODES.get(type(exc), 400))

    @app.exception_handler(RequestValidationError)
    def _request_errors(request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ARG001
        return JSONResponse(status_code=422, content={"ok": False,
                                                      "error": "invalid request: " + "; ".join(".".join(str(part) for part in error["loc"]) + ": " + error["msg"] for error in exc.errors()[:3])})

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.method != "GET":
            origin = request.headers.get("origin") or request.headers.get("referer") or ""
            from urllib.parse import urlparse
            if origin and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse(status_code=403, content={"ok": False, "error": "cross-origin request refused"})
        public = {"/login", "/api/login", "/api/session"}
        if settings.demo_login_enabled and request.url.path not in public and not request.url.path.startswith("/static/"):
            expiry = app.state.sessions.get(request.cookies.get("freight_session"), 0)
            if expiry < time.time():
                if request.url.path.startswith("/api/"):
                    return JSONResponse(status_code=401, content={"error": "Please sign in"})
                return RedirectResponse("/login", status_code=303)
        response = await call_next(request)
        if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
            # A cached copy would show the old pipeline state, or old UI after the app updates itself.
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/api/session")
    def session_status(request: Request):
        return {"authenticated": not settings.demo_login_enabled or app.state.sessions.get(request.cookies.get("freight_session"), 0) > time.time(),
                "demo": settings.demo_login_enabled}

    @app.post("/api/login")
    def login(body: dict[str, Any], response: Response):
        from secrets import compare_digest
        if not (compare_digest(str(body.get("username", "")).encode(), b"admin") and compare_digest(str(body.get("password", "")).encode(), b"1234")):
            return JSONResponse(status_code=401, content={"error": "Incorrect username or password"})
        token = token_urlsafe(32)
        app.state.sessions[token] = time.time() + 8 * 3600
        response.set_cookie("freight_session", token, httponly=True, samesite="lax", max_age=8 * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        app.state.sessions.pop(request.cookies.get("freight_session"), None)
        response.delete_cookie("freight_session")
        return {"ok": True}

    @app.get("/api/notifications")
    def notifications():
        from ..services.gmail_sync import gmail_inbox, queue_status
        from ..services.destination_proposals import list_proposals
        messages = gmail_inbox(settings, 50)
        queue = queue_status(settings)
        items = []
        labels = {"received":"Received", "queued":"Waiting to be read", "analyzing":"Reading now",
                  "draft_ready":"Ready for approval", "ready_for_approval":"Ready for approval",
                  "needs_information":"Information missing", "needs_review":"Needs review", "attachment_unreadable":"Could not read attachment",
                  "no_destination_match":"No destination matched", "not_a_quotation":"Set aside: not a quotation",
                  "extraction_failed":"Could not finish reading", "reply_needs_linking":"Supplier reply needs linking"}
        for message in messages:
            if message["processing_status"] in {"not_a_quotation", "ignored"}: continue
            items.append({"id":"mail-"+message["id"], "time":message["created_at"],
                "title":message["subject"] or "Incoming email", "status":labels.get(message["processing_status"], message["processing_status"].replace("_"," ").title()), "href":"#/inbox"})
        for proposal in list_proposals(settings):
            items.append({"id":"proposal-"+proposal["id"],"time":proposal.get("updated_at"),
                "title":proposal["source_name"]+" · "+proposal["database_name"],
                "status":labels.get(proposal["status"],proposal["status"].replace("_"," ").title()),"href":"#/quotations"})
        from ..db.models import AuditEventRow
        with get_session(settings) as session:
            recent = session.query(AuditEventRow).order_by(AuditEventRow.created_at.desc()).limit(50).all()
            for event in recent:
                items.append({"id":"event-"+event.id,"time":event.created_at,
                    "title":event.action.replace("_", " ").capitalize(),"status":"Action recorded","href":"#/quotations"})
        return {"items":sorted(items,key=lambda item:item["time"] or "",reverse=True)[:100],"queue":queue}

    @app.get("/api/destination-proposals")
    def destination_proposals():
        from ..services.destination_proposals import list_proposals
        return list_proposals(settings)

    @app.post("/api/destination-proposals/{proposal_id}/{operation}")
    def destination_decision(proposal_id: str, operation: str):
        from ..services.destination_proposals import decide
        return decide(settings, proposal_id, operation)

    @app.get("/api/workspace/quotes")
    def workspace_quotes(archived: bool = False, include_intake: bool = False):
        from ..services.workspace import list_quotes
        return list_quotes(settings, archived, include_intake)

    @app.get("/api/workspace/quotes/{quote_id}")
    def workspace_quote(quote_id: str):
        from ..services.workspace import detail
        return detail(settings, quote_id)

    @app.get("/api/workspace/quotes/{quote_id}/sources/{source_id}/download")
    def download_workspace_source(quote_id: str, source_id: str):
        from ..services.common import with_session
        from ..db.models import SourceDocumentRow, IntakeItemRow, GmailAttachmentRow
        with with_session(settings) as repos:
            quotation = repos.get_quotation(quote_id)
            allowed = {str(quotation.intake_item_id)} | {str(reply.intake_item_id) for reply in repos.list_replies(str(quotation.shipment_id)) if reply.quotation_id == quotation.id}
            source = repos.session.get(SourceDocumentRow, source_id)
            if source is not None and source.intake_item_id not in allowed:
                raise NotFoundError("Source document not found")
            intake_id = source.intake_item_id if source else source_id
            if intake_id not in allowed:
                raise NotFoundError("Source document not found")
            intake = repos.get_intake(intake_id)
            filename = source.filename if source else intake.original_filename
            source_path = Path(intake.stored_path) if intake.stored_path else None
            intake_row = repos.session.get(IntakeItemRow, intake_id)
            if source and source.source_kind == "attachment" and intake_row.gmail_message_id:
                attachment = repos.session.query(GmailAttachmentRow).filter_by(message_id=intake_row.gmail_message_id, filename=filename).first()
                source_path = Path(attachment.stored_path) if attachment and attachment.stored_path else None
            elif source and filename != intake.original_filename:
                source_path = None
            if source_path and source_path.is_file():
                return FileResponse(source_path, filename=filename)
            from urllib.parse import quote as urlquote
            name = Path(filename).stem + ".txt"
            return Response(content=source.text if source else intake.raw_text, media_type="text/plain; charset=utf-8",
                            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + urlquote(name)})

    @app.post("/api/workspace/quotes/{quote_id}/{operation}")
    def workspace_operation(quote_id: str, operation: str, request_body: WorkspaceAction = Body(default=WorkspaceAction())) -> dict[str, Any]:
        from ..services import workspace
        body = request_body.model_dump()
        if operation == "archive":
            return workspace.archive(settings, quote_id, body.get("disposition", "archived"), body.get("reason", ""), body.get("reason_code", "other"))
        if operation == "approve":
            return workspace.approve(settings, quote_id)
        if operation == "import":
            return workspace.import_quote(settings, quote_id)
        if operation == "waive-field":
            return workspace.waive_field(settings, quote_id, body.get("field", ""), body.get("reason", ""))
        if operation == "confirmation":
            return workspace.request_confirmation(settings, quote_id)
        if operation == "questions":
            return workspace.start_questions(settings, quote_id)
        if operation == "answer":
            return workspace.answer(settings, quote_id, body.get("turn_id"), body.get("text", ""))
        if operation == "save-draft":
            return workspace.save_local_draft(settings, quote_id, body.get("turn_id"), body.get("recipient", ""), body.get("subject", ""), body.get("body", ""))
        if operation == "draft":
            return workspace.prepare_draft(settings, quote_id, body.get("turn_id"), body.get("recipient", ""), body.get("subject", ""), body.get("body", ""))
        if operation == "resolve":
            return workspace.resolve(settings, quote_id, body.get("evidence_id"), bool(body.get("use_candidate")), body.get("reason", ""))
        raise ValidationFailureError("Unknown quotation operation")

    @app.post("/api/workspace/comparison")
    def workspace_comparison(body: ComparisonQuery) -> dict[str, Any]:
        from ..services.workspace import compare
        return compare(settings, body.shipment_id, body.question, body.pricing_unit)

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
            "database": {"url": "configured", "path": "configured",
                         "initialized": initialized, "schema_version": version},
            "ollama": {**ollama, "model": settings.ollama_model, "base_url": settings.ollama_base_url,
                       "model_installed": settings.ollama_model in models if models else None,
                       "available_models": models},
            "workspace": "configured",
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

    # ------------------------------------------------ supplier databases
    @app.get("/api/supplier-databases")
    def supplier_databases() -> list[dict[str, Any]]:
        from ..services.supplier_databases import list_supplier_databases
        return list_supplier_databases(settings)

    @app.post("/api/supplier-databases/preview", status_code=200)
    async def supplier_database_preview(file: UploadFile = File(...)) -> dict[str, Any]:
        from ..services.supplier_databases import infer_schema
        if not (file.filename or "").lower().endswith(".csv"):
            raise ValidationFailureError("only CSV supplier databases are supported")
        payload = await file.read(_upload_limit(settings) + 1)
        if len(payload) > _upload_limit(settings):
            raise ValidationFailureError(f"CSV is larger than the configured limit of {_upload_limit(settings) // 1024 // 1024} MB")
        with tempfile.TemporaryDirectory(prefix="supplier-preview-") as staging:
            path = Path(staging) / (Path(file.filename or "supplier.csv").name or "supplier.csv")
            path.write_bytes(payload)
            fields, record_count = infer_schema(path)
        return {"csv_filename": Path(file.filename or "supplier.csv").name,
                "record_count": record_count, "schema": fields}

    @app.post("/api/supplier-databases/preview-path", status_code=200)
    def supplier_database_preview_path(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.supplier_databases import preview_supplier_database_path
        return preview_supplier_database_path(settings, str(body.get("path") or ""))

    @app.post("/api/supplier-databases/from-path", status_code=201)
    def supplier_database_connect_path(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.supplier_databases import connect_supplier_database_path
        aliases_value = body.get("routing_aliases") or []
        aliases = ([str(item).strip() for item in aliases_value if str(item).strip()]
                   if isinstance(aliases_value, list)
                   else [item.strip() for item in str(aliases_value).split(",") if item.strip()])
        custom_schema = body.get("schema") if isinstance(body.get("schema"), list) else None
        database = connect_supplier_database_path(
            settings,
            str(body.get("path") or ""),
            name=str(body.get("name") or "").strip(),
            description=str(body.get("description") or "").strip(),
            category=str(body.get("category") or "").strip() or None,
            routing_aliases=aliases,
            schema=custom_schema,
        )
        return {"ok": True, "database": database}

    @app.post("/api/supplier-databases", status_code=201)
    async def supplier_database_connect(
        file: UploadFile = File(...),
        name: str = Form(...),
        description: str = Form(default=""),
        category: str = Form(default=""),
        routing_aliases: str = Form(default=""),
        schema_data: str = Form(default="", alias="schema_json"),
    ) -> dict[str, Any]:
        from ..services.supplier_databases import connect_supplier_database
        payload = await file.read(_upload_limit(settings) + 1)
        if len(payload) > _upload_limit(settings):
            raise ValidationFailureError(f"CSV is larger than the configured limit of {_upload_limit(settings) // 1024 // 1024} MB")
        aliases: list[str] = []
        if routing_aliases.strip():
            try:
                parsed = json.loads(routing_aliases)
                aliases = [str(item).strip() for item in parsed if str(item).strip()] if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                aliases = [item.strip() for item in routing_aliases.split(",") if item.strip()]
        custom_schema: list[dict[str, Any]] | None = None
        if schema_data.strip():
            parsed_schema = json.loads(schema_data)
            if isinstance(parsed_schema, list):
                custom_schema = parsed_schema
        return {"ok": True, "database": connect_supplier_database(
            settings,
            file.filename or "supplier.csv",
            payload,
            name=name,
            description=description,
            category=category or None,
            routing_aliases=aliases,
            schema=custom_schema,
        )}

    @app.get("/api/supplier-databases/{database_id}")
    def supplier_database_detail(database_id: str) -> dict[str, Any]:
        from ..services.supplier_databases import get_supplier_database
        return get_supplier_database(settings, database_id)

    @app.post("/api/supplier-databases/{database_id}/reanalyze", status_code=200)
    def supplier_database_reanalyze(database_id: str) -> dict[str, Any]:
        from ..services.supplier_databases import reanalyze_supplier_database
        return {"ok": True, "database": reanalyze_supplier_database(settings, database_id)}

    @app.patch("/api/supplier-databases/{database_id}")
    def supplier_database_update(database_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.supplier_databases import update_supplier_database
        return {"ok": True, "database": update_supplier_database(settings, database_id, body)}

    @app.delete("/api/supplier-databases/{database_id}")
    def supplier_database_disconnect(database_id: str) -> dict[str, Any]:
        from ..services.supplier_databases import disconnect_supplier_database
        return {"ok": True, "database": disconnect_supplier_database(settings, database_id)}

    # ------------------------------------------------ supplier quotations and agent
    @app.get("/api/supplier-quotations")
    def supplier_quotations() -> dict[str, Any]:
        actions = __import__("freight_agent.services.supplier_workflow", fromlist=["list_actions"]).list_actions(settings)
        action_by_intake = {item["intake_id"]: item for item in actions}
        unprocessed = []
        for document in _document_rows(settings):
            if document["id"] in action_by_intake:
                continue
            unprocessed.append({"id": None, "intake_id": document["id"], "name": document["name"],
                                "file": document["file"],
                                "database_id": None, "database_name": None, "status": "received",
                                "routing": [], "proposed_row": {}, "missing_fields": [], "evidence": {},
                                "notes": "Received quotation is waiting for supplier database routing and analysis.",
                                "created_csv_row": None, "created_at": document["created_at"], "updated_at": document["created_at"]})
        return {"received": unprocessed, "actions": actions}

    @app.post("/api/supplier-quotations/{intake_id}/process")
    def supplier_quotation_process(intake_id: str, database_id: str | None = Query(default=None)) -> dict[str, Any]:
        from ..services.supplier_workflow import run_quotation_pipeline

        outcome = run_quotation_pipeline(settings, intake_id, database_id)
        return {"ok": True, "proposals": outcome.get("proposals", []), "action": outcome["action"], "draft": outcome.get("draft"),
                "notes": outcome["notes"]}

    @app.post("/api/supplier-quotations/{intake_id}/analyze")
    def supplier_quotation_analyze(intake_id: str, database_id: str | None = Query(default=None)) -> dict[str, Any]:
        """The same pipeline as /process, run as a job so the browser keeps painting."""
        from ..services.supplier_workflow import run_quotation_pipeline

        def work(job) -> dict[str, Any]:
            outcome = run_quotation_pipeline(
                settings, intake_id, database_id,
                progress=lambda step: setattr(job, "progress", step))
            return {"proposals": outcome.get("proposals", []), "supplier_action": outcome["action"], "draft": outcome.get("draft"),
                    "notes": outcome["notes"]}

        return {"ok": True, "job": runner().submit("supplier-analysis", intake_id, work,
                                                   max_seconds=_processing_budget_seconds(settings)).as_dict()}

    @app.get("/api/supplier-actions")
    def supplier_actions() -> list[dict[str, Any]]:
        from ..services.supplier_workflow import list_actions
        return list_actions(settings)

    @app.get("/api/supplier-actions/{action_id}")
    def supplier_action_detail(action_id: str) -> dict[str, Any]:
        from ..services.supplier_workflow import get_action
        return get_action(settings, action_id)

    @app.patch("/api/supplier-actions/{action_id}/proposal")
    def supplier_action_proposal(action_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.supplier_workflow import update_proposal
        return {"ok": True, "action": update_proposal(settings, action_id,
                                                       body.get("proposed_row") if isinstance(body.get("proposed_row"), dict) else {},
                                                       body.get("notes"))}

    @app.post("/api/supplier-actions/{action_id}/approve")
    def supplier_action_approve(action_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.supplier_workflow import approve_action
        row = body.get("proposed_row") if isinstance(body.get("proposed_row"), dict) else None
        return {"ok": True, "action": approve_action(settings, action_id, row, body.get("notes"))}

    @app.post("/api/supplier-actions/{action_id}/import")
    def supplier_action_import(action_id: str):
        from ..services.supplier_workflow import import_action
        return {"ok": True, "action": import_action(settings, action_id)}

    @app.post("/api/supplier-actions/{action_id}/create-draft")
    def supplier_action_create_draft(action_id: str) -> dict[str, Any]:
        from ..services.supplier_workflow import create_supplier_review_draft
        return {"ok": True, "draft": create_supplier_review_draft(settings, action_id)}

    @app.post("/api/supplier-actions/{action_id}/status")
    def supplier_action_status(action_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.supplier_workflow import set_action_status
        return {"ok": True, "action": set_action_status(settings, action_id, str(body.get("status") or "needs_human_review"), body.get("notes"))}

    @app.post("/api/supplier-agent/chat")
    def supplier_agent_chat(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.supplier_workflow import chat_supplier_databases
        question = str(body.get("question") or "").strip()
        if not question:
            raise ValidationFailureError("enter a supplier database question")
        return chat_supplier_databases(settings, question, body.get("database_id"))

    @app.get("/api/supplier-agent/history")
    def supplier_agent_history(limit: int = Query(default=30, ge=1, le=100)) -> list[dict[str, Any]]:
        from ..services.supplier_workflow import chat_history
        return chat_history(settings, limit)

    @app.get("/api/review")
    def review_payload() -> dict[str, Any]:
        return build_review(settings)

    # ---------------------------------------------------------------- documents
    @app.post("/api/documents")
    async def upload_documents(files: list[UploadFile] = File(...),
                               on_duplicate: str = Form("error")) -> dict[str, Any]:
        from ..services.intake import register_intake

        if on_duplicate not in {"error", "skip", "reprocess"}:
            raise ValidationFailureError("Choose skip or reprocess for duplicate documents")
        results = []
        for upload in files:
            name = Path((upload.filename or "document").replace("\\", "/")).name
            entry = {"file": name}
            try:
                if name in {"", ".", ".."}:
                    raise ValidationFailureError("Choose a document with a valid filename")
                payload = await upload.read(_upload_limit(settings) + 1)
                if len(payload) > _upload_limit(settings):
                    raise ValidationFailureError(
                        f"File exceeds the configured limit of {_upload_limit(settings) // 1024 // 1024} MB")
                with tempfile.TemporaryDirectory(prefix="freight-upload-") as directory:
                    path = Path(directory) / name
                    path.write_bytes(payload)
                    item, skipped = register_intake(settings, None, path, on_duplicate=on_duplicate)
                entry.update(status="duplicate" if skipped else "registered", intake_id=str(item.id),
                             message="Document already imported" if skipped else "Document received")
            except DuplicateIntakeError as exc:
                entry.update(status="duplicate", intake_id=exc.existing_intake_id,
                             options=["skip", "reprocess"], message="Document already imported")
            except FreightAgentError as exc:
                entry.update(status="rejected", error=str(exc))
            finally:
                await upload.close()
            results.append(entry)
        return {"results": results}

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

    @app.get("/api/documents/{intake_id}")
    def document_detail(intake_id: str) -> dict[str, Any]:
        from ..services.common import with_session
        from ..services.quotation_naming import name_for_intake, name_for_quotation

        with with_session(settings) as repos:
            item = repos.get_intake(intake_id)
            quotes = [q for q in repos.list_quotations(str(item.shipment_id) if item.shipment_id else None)
                      if str(q.intake_item_id) == str(item.id)]
            shipment = repos.get_shipment(str(item.shipment_id)) if item.shipment_id else None
            carriers = {c.id: c.name for c in repos.list_carriers()}
            payload = {
                "id": str(item.id), "name": name_for_intake(repos.session, str(item.id)),
                "file": item.original_filename, "source": str(item.source_type),
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
                dump["name"] = name_for_quotation(repos.session, str(quote.id))
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

        return {"ok": True, "job": runner().submit("process", intake_id, work,
                                                   max_seconds=_processing_budget_seconds(settings)).as_dict()}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        return runner().get(job_id).as_dict()

    @app.get("/api/jobs")
    def jobs_recent() -> list[dict[str, Any]]:
        return runner().recent(20)

    # ------------------------------------------------------------------ review
    @app.delete("/api/workspace/quotation-data")
    def clear_quotation_data() -> dict[str, Any]:
        from ..services.review import clear_quotation_data
        return {"ok": True, "deleted": clear_quotation_data(settings)}

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

    # ------------------------------------------------------------- Gmail integration
    @app.get("/api/gmail/status")
    def gmail_status() -> dict[str, Any]:
        from ..services.gmail_sync import gmail_status
        return gmail_status(settings)

    @app.post("/api/gmail/reconnect")
    def gmail_reconnect_route() -> dict[str, Any]:
        """Re-attach the stored authorisation so a cleared workspace never forces a Google sign-in again."""
        from ..services.gmail_sync import gmail_reconnect
        return {"ok": True, **gmail_reconnect(settings)}

    @app.post("/api/gmail/authorize")
    def gmail_authorize(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.gmail_sync import gmail_authorization_url
        redirect_uri = "http://127.0.0.1:8765/api/gmail/oauth/callback"
        if body.get("redirect_uri") not in (None, "", redirect_uri):
            raise ValidationFailureError("OAuth redirect URI must be exactly the configured callback URI")
        code_verifier = token_urlsafe(64)
        result = gmail_authorization_url(settings, redirect_uri, code_verifier)
        now = time.monotonic()
        app.state.oauth_pending_states = {
            pending_state: pending
            for pending_state, pending in app.state.oauth_pending_states.items()
            if pending[0] > now
        }
        app.state.oauth_pending_states[str(result["state"])] = (now + 600, code_verifier)
        return result

    @app.get("/api/gmail/oauth/callback")
    def gmail_oauth_callback(
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
        error_description: str | None = Query(default=None),  # noqa: ARG001
    ) -> RedirectResponse:
        from ..services.gmail_sync import gmail_oauth_callback

        def oauth_redirect(result: str) -> RedirectResponse:
            return RedirectResponse(url=f"/#/inbox?gmail_error={result}", status_code=303)

        if not state:
            return oauth_redirect("invalid_state")
        pending = app.state.oauth_pending_states.pop(state, None)
        if pending is None:
            return oauth_redirect("invalid_state")
        expires_at, code_verifier = pending
        if time.monotonic() > expires_at:
            return oauth_redirect("expired_state")
        if error:
            return oauth_redirect(error[:80])
        if not code:
            return oauth_redirect("missing_code")

        redirect_uri = "http://127.0.0.1:8765/api/gmail/oauth/callback"
        try:
            gmail_oauth_callback(settings, code, redirect_uri, code_verifier)
        except FreightAgentError:
            return oauth_redirect("authorization_failed")
        except Exception:
            return oauth_redirect("authorization_failed")
        return RedirectResponse(url="/#/inbox?gmail_connected=1", status_code=303)

    @app.post("/api/gmail/sync", status_code=200)
    def gmail_sync(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Synchronize in a job: receive the mailbox now; the background reader then works through it."""
        from ..services.gmail_sync import gmail_sync as sync_mailbox

        def work(job) -> dict[str, Any]:
            job.progress = "receiving new mail…"
            return sync_mailbox(settings, int(body.get("limit") or 25))

        return {"ok": True, "job": runner().submit("gmail-sync", "gmail", work,
                                                   max_seconds=_processing_budget_seconds(settings) * 4).as_dict()}

    @app.get("/api/gmail/inbox")
    def gmail_inbox(limit: int = Query(default=100, ge=1, le=100)) -> list[dict[str, Any]]:
        from ..services.gmail_sync import gmail_inbox
        return gmail_inbox(settings, limit)

    def _analyze_gmail_message(message_row_id: str) -> dict[str, Any]:
        """Analyze mail that arrived before automatic processing existed, in the background."""
        from ..services.gmail_sync import (analyze_inbound_quotation, ensure_message_quotation,
                                           process_inbound_reply)

        def work(job) -> dict[str, Any]:
            job.progress = "reading this quotation…"
            from ..services.gmail_sync import gmail_message_detail
            detail = gmail_message_detail(settings, message_row_id)
            if detail["classification"] == "SUPPLIER_QUOTATION_REPLY":
                return {"status": process_inbound_reply(settings, message_row_id)}
            intake_id = ensure_message_quotation(settings, message_row_id)
            if intake_id is None:
                return {"status": "not_a_quotation"}
            return {"intake_id": intake_id,
                    "status": analyze_inbound_quotation(settings, message_row_id, intake_id)}

        return runner().submit("gmail-message-analysis", message_row_id, work,
                               max_seconds=_processing_budget_seconds(settings) * 2).as_dict()

    @app.get("/api/gmail/messages/{message_row_id}")
    def gmail_message_detail(message_row_id: str) -> dict[str, Any]:
        from ..services.gmail_sync import QUEUE_STATUSES, gmail_message_detail as read_message

        detail = read_message(settings, message_row_id)
        if detail["intake_id"] is None and detail["classification"] in {
                "NEW_QUOTATION", "SUPPLIER_QUOTATION_REPLY", "UNCERTAIN"}:
            # The background reader owns queued and reading mail; opening it must not start a second read.
            if detail["processing_status"] not in {*QUEUE_STATUSES, "analyzing"}:
                run = runner().latest_for(message_row_id)
                if run is None:
                    detail["job"] = _analyze_gmail_message(message_row_id)
                    detail["processing_status"] = "analyzing"
                elif run.active:
                    detail["job"] = run.as_dict()
                    detail["processing_status"] = "analyzing"
        return detail

    @app.post("/api/gmail/messages/{message_row_id}/analyze")
    def gmail_message_analyze(message_row_id: str) -> dict[str, Any]:
        return {"ok": True, "job": _analyze_gmail_message(message_row_id)}

    @app.post("/api/gmail/messages/{message_row_id}/process-reply")
    def gmail_reply_process(message_row_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Merge a reply the mailbox could not link by itself, then re-analyze the quotation."""
        from ..services.gmail_sync import process_inbound_reply

        quotation_id = body.get("quotation_id")

        def work(job) -> dict[str, Any]:
            job.progress = "reading the supplier answer and updating the quotation…"
            return {"status": process_inbound_reply(settings, message_row_id, quotation_id)}

        return {"ok": True, "job": runner().submit("gmail-reply-analysis", message_row_id, work,
                                                   max_seconds=_processing_budget_seconds(settings) * 3).as_dict()}

    @app.post("/api/gmail/messages/{message_row_id}/ensure-quotation")
    def gmail_message_ensure_quotation(message_row_id: str) -> dict[str, Any]:
        from ..services.gmail_sync import ensure_message_quotation
        intake_id = ensure_message_quotation(settings, message_row_id)
        return {"ok": True, "intake_id": intake_id}

    @app.delete("/api/gmail/accounts/{account_id}")
    def gmail_disconnect(account_id: str) -> dict[str, Any]:
        from ..services.gmail_sync import gmail_disconnect
        return gmail_disconnect(settings, account_id)

    @app.get("/api/supplier-email-drafts")
    def supplier_email_drafts() -> list[dict[str, Any]]:
        from ..services.gmail_drafts import list_drafts
        return list_drafts(settings)

    @app.post("/api/supplier-email-drafts", status_code=201)
    def supplier_email_draft_create(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.gmail_drafts import create_draft
        return create_draft(settings, recipient=str(body.get("recipient") or ""), cc=str(body.get("cc") or ""),
                            subject=str(body.get("subject") or ""), body=str(body.get("body") or ""),
                            gmail_account_id=str(body.get("gmail_account_id") or ""),
                            quotation_register_id=body.get("quotation_register_id"),
                            supplier_master_id=body.get("supplier_master_id"),
                            gmail_thread_id=body.get("gmail_thread_id"))

    @app.get("/api/supplier-email-drafts/{draft_id}")
    def supplier_email_draft_detail(draft_id: str) -> dict[str, Any]:
        from ..services.gmail_drafts import get_draft
        return get_draft(settings, draft_id)

    @app.patch("/api/supplier-email-drafts/{draft_id}")
    def supplier_email_draft_save(draft_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.gmail_drafts import save_draft
        return save_draft(settings, draft_id, recipient=str(body.get("recipient") or ""),
                          cc=str(body.get("cc") or ""), subject=str(body.get("subject") or ""),
                          body=str(body.get("body") or ""))

    @app.post("/api/supplier-email-drafts/{draft_id}/approve")
    def supplier_email_draft_approve(draft_id: str) -> dict[str, Any]:
        from ..services.gmail_drafts import approve_draft
        return {"ok": True, "draft": approve_draft(settings, draft_id)}

    @app.post("/api/supplier-email-drafts/{draft_id}/reconcile")
    def reconcile_email_delivery(draft_id: str, body: dict[str, Any]):
        from ..services.gmail_send import reconcile_delivery
        if not isinstance(body.get("delivered"), bool):
            raise ValidationFailureError("Choose whether delivery was confirmed")
        return reconcile_delivery(settings, draft_id, body["delivered"], str(body.get("message_id", "")), str(body.get("reason", "")))

    @app.post("/api/supplier-email-drafts/{draft_id}/send")
    def supplier_email_draft_send(draft_id: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        from ..services.gmail_send import send_approved_draft
        return send_approved_draft(settings, draft_id, str(body.get("confirmation") or ""),
                                   str(body.get("idempotency_key") or ""))

    # ------------------------------------------------------------- Supplier master and register
    @app.get("/api/supplier-master")
    def supplier_master(database_id: str | None = Query(default=None)) -> list[dict[str, Any]]:
        from ..services.supplier_master import list_supplier_master
        return list_supplier_master(settings, database_id)

    @app.post("/api/supplier-master/{database_id}/sync")
    def supplier_master_sync(database_id: str) -> dict[str, Any]:
        from ..services.supplier_master import sync_supplier_master
        return sync_supplier_master(settings, database_id)

    @app.get("/api/quotation-register")
    def quotation_register() -> list[dict[str, Any]]:
        from ..services.quotation_register import list_register
        return list_register(settings)

    @app.post("/api/supplier-actions/{action_id}/import-register")
    def supplier_action_import_register(action_id: str) -> dict[str, Any]:
        from ..services.quotation_register import import_quotation_action
        return {"ok": True, "quotation": import_quotation_action(settings, action_id)}

    # ------------------------------------------------------------- generic verbs
    @app.post("/api/action")
    def action(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return {"ok": True, **apply_action(settings, str(body.get("action") or ""), body)}

    @app.on_event("shutdown")
    def stop_gmail_monitor() -> None:
        app.state.gmail_stop_event.set()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.state.gmail_stop_event = threading.Event()
    from ..services.gmail_sync import start_background_monitor
    app.state.gmail_monitor_thread = start_background_monitor(settings, app.state.gmail_stop_event) if settings.gmail_background_enabled else None

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


__all__ = ["JobRunner", "create_app"]
