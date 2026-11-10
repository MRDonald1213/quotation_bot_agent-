from sqlalchemy import text

from freight_agent.db.engine import get_session
from freight_agent.db.migrations import init_schema, schema_status
from freight_agent.db.models import SchemaVersionRow


def test_v15_migration_adds_requested_field_tracking(settings):
    with get_session(settings) as session:
        session.execute(text("ALTER TABLE supplier_email_drafts DROP COLUMN requested_fields_json"))
        session.query(SchemaVersionRow).filter(SchemaVersionRow.version == 15).delete()
        session.commit()

        assert init_schema(session) == ["v15"]
        columns = {row["name"] for row in session.execute(
            text("PRAGMA table_info(supplier_email_drafts)")
        ).mappings()}
        assert "requested_fields_json" in columns
        assert schema_status(session) == (True, 15)
