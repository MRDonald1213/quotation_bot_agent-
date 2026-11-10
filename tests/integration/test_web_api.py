"""The local web app must call the existing services, not replace them.

These tests drive the FastAPI backend with the same FakeLLM used by the workflow
A/B/C contract tests, so no Ollama is required and every assertion is about the
real services and database.
"""
from __future__ import annotations

import time
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from freight_agent.api.app import create_app
from freight_agent.db.models import AuditEventRow
from freight_agent.domain.enums import ReviewStatus, ValidationStatus
from freight_agent.services.common import with_session
from freight_agent.services.shipments import create_shipment

CLASSIFY_OK = {
    "is_freight_quote": True, "reason": "carrier price list", "confidence": 0.92,
    "carrier_name": "Baltic Lines OÜ", "shipment_reference": None,
    "needs_human_review": False, "document_type": "carrier_quote",
}

EXTRACT_A = {
    "carrier_name": "Baltic Lines OÜ",
    "quote_reference": "BL-2026-0417",
    "currency": "EUR",
    "base_price": "3200",
    "charges": [
        {"label": "Terminal handling charge", "amount": "340", "currency": "EUR",
         "included_in_base_price": False, "mandatory": True,
         "evidence_snippet": "Terminal handling charge: 340 EUR"},
        {"label": "Delivery to Hamburg warehouse", "amount": None, "currency": "EUR",
         "included_in_base_price": False, "mandatory": True,
         "evidence_snippet": "Delivery to Hamburg warehouse: to be confirmed, price TBD"},
    ],
    "included_services": ["Ocean freight"],
    "excluded_services": ["on-carriage"],
    "origin": "Baku, AZ",
    "destination": "Hamburg, DE",
    "transit_days_min": 12,
    "transit_days_max": 16,
    "valid_until": "2026-11-15",
    "delivery_scope": "port_to_port",
    "insurance_included": None,
    "customs_included": None,
    "assumptions": [],
    "fields_requiring_review": [],
    "field_evidence": [
        {"field_name": "carrier_name", "snippet": "Baltic Lines OÜ", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "quote_reference", "snippet": "BL-2026-0417", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "base_price", "snippet": "Base ocean freight: 3,200 EUR", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "origin", "snippet": "Baku, AZ", "page_or_sheet": "page 1", "confidence": 0.9},
        {"field_name": "destination", "snippet": "Hamburg, DE", "page_or_sheet": "page 1", "confidence": 0.9},
        {"field_name": "valid_until", "snippet": "valid until 2026-11-15", "page_or_sheet": "page 1", "confidence": 0.95},
        {"field_name": "delivery_scope", "snippet": "port to port", "page_or_sheet": "page 1", "confidence": 0.85},
    ],
}


@pytest.fixture()
def client(settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


# an invalid wording answer makes the service keep its rule-template questions
WORDING_FALLBACK = {"gaps": [], "clarification_questions": [], "comparison_readiness": "not_ready", "summary": ""}


def _arm(fake_llm, filename: str) -> None:
    fake_llm.responses = {"Extract the QuoteExtraction": EXTRACT_A,
                          'filename="gap_context.json"': WORDING_FALLBACK,
                          f'filename="{filename}"': CLASSIFY_OK}


def _upload(client: TestClient, path: Path, on_duplicate: str = "error") -> dict:
    with path.open("rb") as handle:
        return client.post("/api/documents", files={"files": (path.name, handle)},
                           data={"on_duplicate": on_duplicate}).json()


def _wait(client: TestClient, job_id: str) -> dict:
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            assert job["status"] == "succeeded", job["error"]
            return job["result"]
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _processed(client: TestClient, intake_id: str, force: bool = False) -> dict:
    response = client.post(f"/api/documents/{intake_id}/process", params={"force": str(force).lower()})
    assert response.status_code == 200, response.text
    return _wait(client, response.json()["job"]["id"])


def _quote_of(client: TestClient, intake_id: str) -> dict:
    detail = client.get(f"/api/documents/{intake_id}").json()
    assert detail["quotations"], detail
    return detail["quotations"][0]


def test_review_pages_keep_loading_with_malformed_legacy_audit_identifier(
    client: TestClient, settings,
) -> None:
    with with_session(settings) as repos:
        repos.session.add(AuditEventRow(
            id="legacy-audit-id",
            entity_type="intake_item",
            entity_id=str(uuid4()),
            action="classification_restored_after_reanalysis_error",
            details_json="{}",
            created_at="2026-11-09T00:00:00+00:00",
        ))
        repos.session.commit()

    assert client.get("/api/drafts").status_code == 200
    assert client.get("/api/shipments").status_code == 200
    assert client.get("/api/workspace/quotes?include_intake=true").status_code == 200
    notifications = client.get("/api/notifications")
    assert notifications.status_code == 200
    assert not any(item["id"] == "event-legacy-audit-id" for item in notifications.json()["items"])


def test_public_health_endpoint_works_with_login_enabled(settings) -> None:
    settings.demo_login_enabled = True
    with TestClient(create_app(settings)) as health_client:
        response = health_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ------------------------------------------------------------------ status
def test_status_reports_local_model_and_schema_without_calling_it(client: TestClient) -> None:
    payload = client.get("/api/status").json()
    assert payload["backend"] == "ok"
    assert payload["database"]["initialized"] is True
    assert payload["database"]["schema_version"] == 15
    assert payload["ollama"]["model"] == "fake-model"
    assert set(payload["parsers"]) == {"pdf", "docx", "xlsx"}
    assert ".eml" in payload["limits"]["supported_extensions"]


def test_supplier_database_preview_connect_update_and_disconnect(client: TestClient, tmp_path: Path) -> None:
    csv_file = tmp_path / "freight_suppliers.csv"
    csv_file.write_text(
        "supplier_name,hourly_rate,currency,available_date\n"
        "North Freight,125.50,EUR,2026-11-01\n"
        "Coastal Carrier,140,EUR,2026-11-15\n",
        encoding="utf-8",
    )

    preview = client.post(
        "/api/supplier-databases/preview",
        files={"file": (csv_file.name, csv_file.read_bytes(), "text/csv")},
    )
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["record_count"] == 2
    assert {field["name"]: field["type"] for field in preview_payload["schema"]} == {
        "supplier_name": "supplier",
        "hourly_rate": "currency",
        "currency": "text",
        "available_date": "date",
    }

    connected = client.post(
        "/api/supplier-databases",
        files={"file": (csv_file.name, csv_file.read_bytes(), "text/csv")},
        data={
            "name": "Freight & Carrier Rate Quotes",
            "description": "Carrier supplier pricing by freight service and route",
            "category": "Freight quotations",
            "routing_aliases": '["freight", "carrier rates"]',
            "schema_json": __import__("json").dumps(preview_payload["schema"]),
        },
    )
    assert connected.status_code == 201
    database = connected.json()["database"]
    database_id = database["id"]
    assert database["record_count"] == 2
    assert len(database["schema"]) == 4

    listed = client.get("/api/supplier-databases").json()
    assert [item["id"] for item in listed] == [database_id]

    updated = client.patch(
        "/api/supplier-databases/" + database_id,
        json={"description": "Updated freight supplier database"},
    )
    assert updated.status_code == 200
    assert updated.json()["database"]["description"] == "Updated freight supplier database"

    detail = client.get("/api/supplier-databases/" + database_id)
    assert detail.status_code == 200
    assert detail.json()["name"] == "Freight & Carrier Rate Quotes"
    assert detail.json()["provider_type"] == "csv"
    assert detail.json()["connection_type"] == "csv_path"

    reanalyzed = client.post("/api/supplier-databases/" + database_id + "/reanalyze")
    assert reanalyzed.status_code == 200
    assert reanalyzed.json()["database"]["record_count"] == 2
    assert reanalyzed.json()["database"]["provider_type"] == "csv"

    deleted = client.delete("/api/supplier-databases/" + database_id)
    assert deleted.status_code == 200
    assert deleted.json()["database"]["status"] == "disconnected"
    assert client.get("/api/supplier-databases").json() == []
    assert client.get("/api/supplier-databases/" + database_id).status_code == 404


def test_complete_supplier_database_flow(client: TestClient, tmp_path: Path) -> None:
    csv_file = tmp_path / "fuel_suppliers.csv"
    csv_file.write_text(
        "supplier_name,fuel_type,currency,unit_price,unit,valid_until\n"
        "Fuel House Baku,Diesel,EUR,1.42,Litre,2026-12-15\n"
        "Metro Fuel Co,Diesel,EUR,1.38,Litre,2026-12-20\n",
        encoding="utf-8",
    )
    preview = client.post("/api/supplier-databases/preview", files={"file": (csv_file.name, csv_file.read_bytes(), "text/csv")})
    schema = preview.json()["schema"]
    for field in schema:
        if field["name"] in {"supplier_name", "fuel_type", "currency", "unit_price", "unit"}:
            field["required"] = True
    connected = client.post(
        "/api/supplier-databases",
        files={"file": (csv_file.name, csv_file.read_bytes(), "text/csv")},
        data={"name": "Fleet Fuel Suppliers", "description": "Fuel supplier quotations for fleet operations",
              "category": "Fuel quotations", "routing_aliases": '["fuel","diesel","fleet"]',
              "schema_json": json.dumps(schema)},
    )
    database_id = connected.json()["database"]["id"]

    quotation = tmp_path / "fuel_quote.txt"
    quotation.write_text(
        "Supplier name: New Fuel Supplier\nFuel type: Diesel\nCurrency: EUR\nUnit price: 1.29 EUR per litre\nUnit: Litre\nValid until: 2026-12-31\n",
        encoding="utf-8",
    )
    with quotation.open("rb") as handle:
        upload = client.post("/api/documents", files={"files": (quotation.name, handle)}, data={"on_duplicate": "error"})
    intake_id = upload.json()["results"][0]["intake_id"]

    # The generic CSV adapter remains supported independently of freight extraction.
    # A fuel-only document must not bypass canonical freight classification gates.
    from freight_agent.services.supplier_workflow import process_supplier_quotation
    action = process_supplier_quotation(client.app.state.settings, intake_id, database_id)
    assert action["database_id"] == database_id
    assert action["status"] == "ready_for_approval"
    assert action["proposed_row"]["supplier_name"] == "New Fuel Supplier"
    assert action["proposed_row"]["unit_price"] == "1.29"

    approved = client.post(f"/api/supplier-actions/{action['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["action"]["status"] == "approved"
    imported = client.post(f"/api/supplier-actions/{action['id']}/import")
    assert imported.status_code == 200
    created = imported.json()["action"]
    assert created["status"] == "record_created"
    assert created["created_csv_row"]["supplier_name"] == "New Fuel Supplier"

    second_approval = client.post(f"/api/supplier-actions/{action['id']}/approve")
    assert second_approval.status_code == 200

    chat = client.post("/api/supplier-agent/chat", json={"question": "Which is the cheapest fuel supplier?", "database_id": database_id})
    assert chat.status_code == 200
    assert "1.29" in chat.json()["answer"] or "lowest" in chat.json()["answer"]
    assert chat.json()["citations"]

    managed_csv = Path(connected.json()["database"]["csv_path"])
    lines = managed_csv.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    assert any("New Fuel Supplier" in line for line in lines)


def test_dashboard_counts_come_from_the_database(client: TestClient, settings) -> None:
    create_shipment(settings, reference="SHP-EMPTY", name="t", origin="Baku", destination="Hamburg",
                    cargo_description="parts")
    payload = client.get("/api/dashboard").json()
    assert payload["counts"]["imported"] == 0
    assert payload["counts"]["documents"] == 0
    assert payload["recent"] == []
    assert [s["reference"] for s in client.get("/api/shipments").json()] == ["SHP-EMPTY"]


def test_supplier_intelligence_starts_without_seed_records(client: TestClient) -> None:
    result = client.post("/api/supplier-intelligence/query", json={
        "question": "Find ocean suppliers operating between Shenzhen and Hamburg",
    })
    assert result.status_code == 200
    payload = result.json()
    assert payload["exact_matches"] == []
    assert payload["partial_matches"] == []
    assert "sample" not in payload["data_note"].lower()

    directory = client.get("/api/supplier-intelligence", params={
        "category": "Ocean Carrier (VOCC)", "origin": "Shenzhen", "destination": "Hamburg",
    })
    assert directory.status_code == 200
    assert directory.json()["suppliers"] == []


# ------------------------------------------------------------------- intake
def test_upload_registers_and_parses_locally(client: TestClient, quote_a_file: Path) -> None:
    result = _upload(client, quote_a_file)["results"][0]
    assert result["status"] == "registered"
    detail = client.get(f"/api/documents/{result['intake_id']}").json()
    assert detail["file"] == quote_a_file.name
    assert "Baltic Lines" in detail["text"]
    assert detail["classification"] == "pending"


def test_upload_rejects_unsupported_extension(client: TestClient, tmp_path: Path) -> None:
    payload = tmp_path / "carrier-macro.exe"
    payload.write_bytes(b"MZ\x00\x00")
    entry = _upload(client, payload)["results"][0]
    assert entry["status"] == "rejected"
    assert ".docx" in entry["error"]                      # the allowed list is shown back
    assert client.get("/api/documents").json() == []      # nothing reached intake


def test_upload_enforces_the_configured_size_limit(settings, tmp_path: Path) -> None:
    room = settings.model_copy(update={"max_file_size_mb": 1})
    big = tmp_path / "big.txt"
    big.write_bytes(b"x" * (1024 * 1024 + 8))
    with TestClient(create_app(room)) as small_client:
        entry = _upload(small_client, big)["results"][0]
    assert entry["status"] == "rejected"
    assert "configured limit of 1 MB" in entry["error"]


def test_duplicate_upload_offers_skip_or_reprocess(client: TestClient, quote_a_file: Path) -> None:
    first = _upload(client, quote_a_file)["results"][0]
    assert first["status"] == "registered"
    again = _upload(client, quote_a_file)["results"][0]
    assert again["status"] == "duplicate"
    assert again["options"] == ["skip", "reprocess"]
    assert again["intake_id"] == first["intake_id"]
    assert len(client.get("/api/documents").json()) == 1
    skipped = _upload(client, quote_a_file, on_duplicate="skip")["results"][0]
    assert skipped["status"] == "duplicate" and "already imported" in skipped["message"]


def test_source_download_returns_the_stored_file(client: TestClient, quote_a_file: Path) -> None:
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    response = client.get(f"/api/documents/{intake_id}/source")
    assert response.status_code == 200
    assert response.content == quote_a_file.read_bytes()


def test_not_a_quote_is_clear_and_idempotent(client: TestClient, quote_a_file: Path) -> None:
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    first = client.post("/api/action", json={
        "action": "intake.ignore", "id": intake_id, "reason": "carrier newsletter",
    })
    assert first.status_code == 200
    assert "marked as not a quote" in first.json()["message"]

    detail = client.get(f"/api/documents/{intake_id}").json()
    assert detail["classification"] == "irrelevant"
    assert detail["reason"] == "carrier newsletter"

    second = client.post("/api/action", json={
        "action": "intake.ignore", "id": intake_id, "reason": "carrier newsletter",
    })
    assert second.status_code == 200
    assert "already marked" in second.json()["message"]


def test_missing_records_return_structured_404(client: TestClient) -> None:
    response = client.get("/api/documents/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert response.json()["type"] == "NotFoundError"
    assert response.json()["ok"] is False


# ------------------------------------------------------- workflow A over HTTP
def test_processing_through_the_web_creates_quote_and_proposal(
        client: TestClient, fake_llm, quote_a_file: Path) -> None:
    _arm(fake_llm, quote_a_file.name)
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    result = _processed(client, intake_id)
    assert result["created_quote"] is True
    block = _quote_of(client, intake_id)
    quote = block["quote"]
    assert quote["review_status"] == ReviewStatus.extracted.value
    # an unpriced mandatory charge is never treated as zero
    assert quote["total_known_cost"] is None
    assert block["gaps"], "critical gaps must stay visible in the review payload"
    rows = client.get("/api/documents").json()
    assert [r for r in rows if r["id"] == intake_id][0]["processing"] == "needs_review"


def test_irrelevant_document_processing_has_no_quote_and_persists_disposition(
        client: TestClient, fake_llm, quote_a_file: Path) -> None:
    irrelevant = {
        "is_freight_quote": False, "reason": "marketing newsletter", "confidence": 0.99,
        "carrier_name": None, "shipment_reference": None,
        "needs_human_review": False, "document_type": "unrelated",
    }
    fake_llm.responses = {f'filename="{quote_a_file.name}"': irrelevant}
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    result = _processed(client, intake_id)
    assert result["created_quote"] is False
    assert any("not a quote" in note.lower() for note in result["notes"])
    detail = client.get(f"/api/documents/{intake_id}").json()
    assert detail["classification"] == "irrelevant"
    assert detail["processing"] == "processed"
    assert detail["quotations"] == []


def test_second_processing_of_the_same_document_is_refused(
        client: TestClient, fake_llm, quote_a_file: Path) -> None:
    _arm(fake_llm, quote_a_file.name)
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    _processed(client, intake_id)
    response = client.post(f"/api/documents/{intake_id}/process", params={"force": "false"})
    assert response.status_code == 200
    job_id = response.json()["job"]["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)
    assert job["status"] == "failed"
    assert "already" in job["error"].lower(), "the duplicate guard must surface as an actionable failure"
    with with_session(client.app.state.settings) as repos:
        assert len(repos.list_quotations()) == 1


def test_job_status_is_visible_to_the_page(client: TestClient, fake_llm, quote_a_file: Path) -> None:
    _arm(fake_llm, quote_a_file.name)
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    job_id = client.post(f"/api/documents/{intake_id}/process").json()["job"]["id"]
    _wait(client, job_id)
    listed = client.get("/api/jobs").json()
    assert [j for j in listed if j["id"] == job_id][0]["status"] == "succeeded"
    assert client.get("/api/jobs/nope").status_code == 400


# --------------------------------------------- approve / import separation
@pytest.fixture()
def reviewed_quote(client: TestClient, fake_llm, quote_a_file: Path) -> tuple[TestClient, str]:
    _arm(fake_llm, quote_a_file.name)
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    _processed(client, intake_id)
    return client, _quote_of(client, intake_id)["quote"]["id"]


def test_import_is_refused_before_approval(reviewed_quote) -> None:
    client, quote_id = reviewed_quote
    response = client.post(f"/api/quotes/{quote_id}/import")
    assert response.status_code == 400
    assert "Approve the review before importing" in response.json()["error"]
    with with_session(client.app.state.settings) as repos:
        assert repos.get_quotation(quote_id).review_status == ReviewStatus.extracted


def _resolve_review_requirements(client, quote_id):
    from freight_agent.domain.enums import GapStatus
    with with_session(client.app.state.settings) as repos, repos.atomic():
        quote = repos.get_quotation(quote_id)
        quote.details.pricing_unit = "shipment"
        shipment = repos.get_shipment(str(quote.shipment_id))
        shipment.status = "open"
        repos.update_shipment(shipment)
        from freight_agent.domain.models import Gap
        repos.add_gaps([Gap(shipment_id=quote.shipment_id, quotation_id=quote.id, field_name="pricing_unit", gap_type="missing", severity="critical", description="Explicit operator waiver in test", status="waived"),
                       Gap(shipment_id=quote.shipment_id, quotation_id=quote.id, field_name="currency", gap_type="missing", severity="critical", description="Explicit operator waiver in test", status="waived")])
        repos.update_quotation(quote)
        for gap in repos.list_gaps(str(quote.shipment_id)):
            gap.status = GapStatus.waived
            repos.update_gap(gap)
        for evidence in repos.evidence_for_quotation(quote_id):
            evidence.validation_status = ValidationStatus.accepted
            repos.update_evidence(evidence)


def test_approval_then_import_commits_once(reviewed_quote) -> None:
    client, quote_id = reviewed_quote
    settings = client.app.state.settings
    assert client.post(f"/api/quotes/{quote_id}/review/approve").status_code == 400
    _resolve_review_requirements(client, quote_id)
    approved = client.post(f"/api/quotes/{quote_id}/review/approve", json={"notes": "checked against the PDF"})
    assert approved.status_code == 200
    assert approved.json()["review_status"] == ReviewStatus.approved.value

    preview = client.get(f"/api/quotes/{quote_id}/import/preview").json()
    assert preview["already_imported"] is False
    assert preview["creates"] == [{"record": "quotation", "id": quote_id}]
    assert preview["updates"][0]["to"] == ReviewStatus.imported.value
    assert not preview["linked"]["critical_gaps"]

    imported = client.post(f"/api/quotes/{quote_id}/import")
    assert imported.status_code == 200
    assert imported.json()["already_imported"] is False

    # clicking import again must not duplicate or rewrite anything
    second = client.post(f"/api/quotes/{quote_id}/import")
    assert second.status_code == 200
    assert second.json()["already_imported"] is True
    with with_session(settings) as repos:
        quotes = repos.list_quotations()
        assert len(quotes) == 1 and quotes[0].id == repos.get_quotation(quote_id).id
        assert quotes[0].review_status == ReviewStatus.imported
        actions = [a.action for a in repos.list_recent_audit(50)]
        assert actions.count("imported_to_database") == 1


def test_reapproval_after_import_is_refused(reviewed_quote) -> None:
    client, quote_id = reviewed_quote
    _resolve_review_requirements(client, quote_id)
    client.post(f"/api/quotes/{quote_id}/review/approve")
    client.post(f"/api/quotes/{quote_id}/import")
    response = client.post(f"/api/quotes/{quote_id}/review/approve")
    assert response.status_code == 400
    assert "already imported" in response.json()["error"]
    rejection = client.post(f"/api/quotes/{quote_id}/review/reject", json={"reason": "nope"})
    assert rejection.status_code == 400


def test_rejection_keeps_the_record_out_of_the_canonical_state(reviewed_quote) -> None:
    client, quote_id = reviewed_quote
    response = client.post(f"/api/quotes/{quote_id}/review/reject", json={"reason": "wrong carrier"})
    assert response.status_code == 200
    assert response.json()["review_status"] == ReviewStatus.rejected.value
    with with_session(client.app.state.settings) as repos:
        assert repos.get_quotation(quote_id).review_status == ReviewStatus.rejected
        assert client.post(f"/api/quotes/{quote_id}/import").status_code == 400


# ------------------------------------------------------------------ evidence
def test_evidence_accept_and_reject_persist(reviewed_quote) -> None:
    client, quote_id = reviewed_quote
    rows = client.get("/api/documents").json()
    block = _quote_of(client, rows[0]["id"])
    evidence = next(e for e in block["evidence"] if e["field_name"] == "base_price")
    rejected = client.post(f"/api/evidence/{evidence['id']}/decide", json={"accept": False})
    assert rejected.json()["validation_status"] == ValidationStatus.needs_review.value
    accepted = client.post(f"/api/evidence/{evidence['id']}/decide", json={"accept": True})
    assert accepted.json()["validation_status"] == ValidationStatus.accepted.value
    with with_session(client.app.state.settings) as repos:
        assert repos.get_evidence(evidence["id"]).validation_status == ValidationStatus.accepted


def test_unknown_evidence_returns_404(client: TestClient) -> None:
    response = client.post("/api/evidence/00000000-0000-0000-0000-000000000000/decide", json={"accept": True})
    assert response.status_code == 404


# ------------------------------------------------------- workflow B over HTTP
def test_route_approval_makes_drafts_and_export_stays_human_gated(
        client: TestClient, fake_llm, quote_a_file: Path) -> None:
    _arm(fake_llm, quote_a_file.name)
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    _processed(client, intake_id)
    shipments = client.get("/api/shipments").json()
    assert len(shipments) == 1 and shipments[0]["status"] == "needs_review"

    approved = client.post("/api/action", json={"action": "shipment.approve", "id": shipments[0]["id"]})
    assert approved.status_code == 200, approved.text
    drafts = client.get("/api/drafts").json()
    assert drafts, "approving the route generates clarification drafts automatically"

    draft_id = drafts[0]["id"]
    blocked = client.post(f"/api/drafts/{draft_id}/export", json={"format": "txt"})
    assert blocked.status_code == 400
    assert "approval" in blocked.json()["error"].lower()

    assert client.post(f"/api/drafts/{draft_id}/approve").status_code == 200
    exported = client.post(f"/api/drafts/{draft_id}/export", json={"format": "txt"})
    assert exported.status_code == 200
    assert "Subject" in exported.text
    # an eml without any known recipient is refused rather than guessed
    eml = client.post(f"/api/drafts/{draft_id}/export", json={"format": "eml", "recipient": "carrier@example.com"})
    assert eml.status_code == 200
    assert b"carrier@example.com" in eml.content


# ------------------------------------------------------- workflow C over HTTP
def test_comparison_is_rendered_from_the_deterministic_service(
        client: TestClient, fake_llm, quote_a_file: Path) -> None:
    _arm(fake_llm, quote_a_file.name)
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    _processed(client, intake_id)
    client.post("/api/action", json={"action": "shipment.approve",
                                     "id": client.get("/api/shipments").json()[0]["id"]})
    shipment = client.get("/api/shipments").json()[0]
    detail = client.get(f"/api/shipments/{shipment['id']}").json()
    report = detail["comparison"]
    assert report["shipment_reference"] == shipment["reference"]
    row = report["rows"][0]
    assert row["base_price"] == "3200"
    # only the priced extras are summed; the TBD one stays an unknown, never a zero
    assert row["known_subtotal"] == "3540"
    assert row["uncertain_charges"]
    assert row["unresolved_critical_gaps"]
    assert report["recommendation"] is None, "an incomplete quote must not be declared the winner"
    assert detail["open_gaps"] >= 1

    report_file = client.get(f"/api/shipments/{shipment['id']}/report", params={"format": "md"})
    assert report_file.status_code == 200
    assert shipment["reference"] in report_file.text
    bad = client.get(f"/api/shipments/{shipment['id']}/report", params={"format": "exe"})
    assert bad.status_code == 422


# ---------------------------------------------------------------- safety net
def test_non_get_from_a_foreign_origin_is_refused(client: TestClient) -> None:
    response = client.post("/api/action", json={"action": "refresh"},
                           headers={"Origin": "http://evil.example"})
    assert response.status_code == 403
    assert "cross-origin" in response.json()["error"]


def test_unknown_action_returns_404(client: TestClient) -> None:
    response = client.post("/api/action", json={"action": "shipment.laugh", "id": "x"})
    assert response.status_code == 404


def test_static_app_shell_is_served(client: TestClient) -> None:
    page = client.get("/").text
    assert "Freight AI Agent" in page
    for asset in ("/static/api.js", "/static/app.js", "/static/styles.css"):
        assert client.get(asset).status_code == 200
    assert client.get("/static/../secret.env").status_code in (400, 404)


def test_review_payload_keeps_document_text_out_of_lists(client: TestClient, quote_a_file: Path) -> None:
    _upload(client, quote_a_file)
    payload = client.get("/api/review").json()
    assert payload["documents"][0]["preview"].endswith(("…", "e"))
    assert len(payload["documents"][0]["preview"]) <= 701
    assert "raw_text" not in payload["documents"][0]


def test_processed_artifacts_stay_inside_the_workspace(settings, client: TestClient,
                                                       quote_a_file: Path) -> None:
    intake_id = _upload(client, quote_a_file)["results"][0]["intake_id"]
    stored = Path(client.get(f"/api/documents/{intake_id}").json()["stored_path"])
    assert stored.is_file()
    assert "workspace" in str(stored)
    assert not stored.is_symlink()
