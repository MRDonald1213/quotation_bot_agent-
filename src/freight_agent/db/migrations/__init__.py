from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ...domain.exceptions import DatabaseError
from ..models import SCHEMA_VERSION, Base, SchemaVersionRow, utcnow_iso
from ..repositories import Repos


def init_schema(session: Session) -> list[str]:
    """Apply additive migrations and baseline history in one transaction."""
    connection = session.connection()
    versions = (
        session.scalars(select(SchemaVersionRow.version)).all()
        if inspect(connection).has_table("schema_version") else []
    )
    current = max(versions, default=0)
    if current > SCHEMA_VERSION:
        raise DatabaseError(
            f"database schema v{current} is newer than supported v{SCHEMA_VERSION}"
        )
    if current == SCHEMA_VERSION:
        return []

    applied: list[str] = []
    repos = Repos(session)
    with repos.atomic():
        # SQLite's legacy transaction mode does not BEGIN for reads or DDL.
        # Use this same connection so table creation rolls back with snapshots.
        if connection.dialect.name == "sqlite":
            dbapi_connection = connection.connection.driver_connection
            if not dbapi_connection.in_transaction:
                connection.exec_driver_sql("BEGIN")
        Base.metadata.create_all(connection)
        if current < 3:
            shipment_columns = {
                column["name"] for column in inspect(connection).get_columns("shipments")
            }
            for name, definition in (
                ("origin_country", "VARCHAR(2)"),
                ("destination_country", "VARCHAR(2)"),
                ("route_category", "VARCHAR(6)"),
                ("source_intake_item_id", "VARCHAR(36)"),
                ("route_evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("approved_at", "VARCHAR(32)"),
            ):
                if name not in shipment_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE shipments ADD COLUMN {name} {definition}"
                    )
        if current < 1:
            session.add(SchemaVersionRow(
                version=1, applied_at=utcnow_iso(),
                description="schema v1: core tables for Workflow A intake-to-quote",
            ))
            applied.append("v1")
        if current < 2:
            for quote in repos.list_quotations():
                repos.record_quotation_version(
                    quote, repos.evidence_for_quotation(str(quote.id)),
                )
            session.add(SchemaVersionRow(
                version=2, applied_at=utcnow_iso(),
                description="schema v2: Workflow B clarification, replies and quotation history",
            ))
            applied.append("v2")
        if current < 3:
            session.add(SchemaVersionRow(
                version=3, applied_at=utcnow_iso(),
                description="schema v3: shipment route countries, provenance and human review",
            ))
            applied.append("v3")
        if current < 4:
            quotation_columns = {
                column["name"] for column in inspect(connection).get_columns("quotations")
            }
            for name, definition in (
                ("review_status", "VARCHAR(16) NOT NULL DEFAULT 'extracted'"),
                ("reviewed_at", "VARCHAR(32)"),
                ("imported_at", "VARCHAR(32)"),
                ("review_notes", "TEXT"),
            ):
                if name not in quotation_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE quotations ADD COLUMN {name} {definition}"
                    )
            # quotations recorded before this split were already reviewed through the CLI
            connection.exec_driver_sql(
                "UPDATE quotations SET review_status = 'imported',"
                " imported_at = COALESCE(updated_at, created_at) WHERE reviewed_at IS NULL"
            )
            session.add(SchemaVersionRow(
                version=4, applied_at=utcnow_iso(),
                description="schema v4: quotation review and explicit database import state",
            ))
            applied.append("v4")
        if current < 5:
            session.add(SchemaVersionRow(
                version=5, applied_at=utcnow_iso(),
                description="schema v5: connected supplier databases and CSV schemas",
            ))
            applied.append("v5")
        if current < 6:
            session.add(SchemaVersionRow(
                version=6, applied_at=utcnow_iso(),
                description="schema v6: supplier quotation actions and chat history",
            ))
            applied.append("v6")
        if current < 7:
            session.add(SchemaVersionRow(
                version=7, applied_at=utcnow_iso(),
                description="schema v7: supplier master, quotation register and Gmail workflow records",
            ))
            applied.append("v7")
    return applied


def schema_status(session: Session) -> tuple[bool, int | None]:
    try:
        versions = session.scalars(select(SchemaVersionRow.version)).all()
    except Exception:  # noqa: BLE001
        raise DatabaseError("schema_version table missing; run `freight init`") from None
    return bool(versions), max(versions) if versions else None
