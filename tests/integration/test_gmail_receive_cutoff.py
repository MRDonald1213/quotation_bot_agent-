from freight_agent.db.engine import get_session
from freight_agent.db.models import GmailAccountRow, GmailMessageRow, SchemaVersionRow
from freight_agent.db.migrations import init_schema
from freight_agent.services.gmail_sync import _claim_next_queued, gmail_inbox, queue_status


def _seed_account(settings, *, cutoff=None, last_sync="2026-11-09T02:00:00+00:00"):
    with get_session(settings) as session:
        session.add(GmailAccountRow(
            id="mail-account", email="ops@example.test", status="connected",
            sync_after_at=cutoff, last_synced_at=last_sync,
            created_at="2026-11-08T01:00:00+00:00", updated_at="2026-11-09T02:00:00+00:00",
        ))
        session.flush()
        for ident, sent_at in (
            ("old", "2026-11-08T23:59:00+00:00"),
            ("new", "2026-11-09T02:01:00+00:00"),
        ):
            session.add(GmailMessageRow(
                id=ident, gmail_message_id=ident, gmail_thread_id=None,
                account_id="mail-account", sender="carrier@example.test", recipients_json="[]",
                subject=f"Quotation {ident}", body_text="Quote details", sent_at=sent_at,
                classification="NEW_QUOTATION", processing_status="received",
                source_hash=ident, created_at="2026-11-09T02:02:00+00:00",
            ))
        session.commit()


def test_mailbox_hides_cached_messages_before_latest_sync_and_keeps_new_messages(settings):
    _seed_account(settings, cutoff=None)

    rows = gmail_inbox(settings)

    assert [row["id"] for row in rows] == ["new"]
    with get_session(settings) as session:
        account = session.get(GmailAccountRow, "mail-account")
        assert account.sync_after_at == account.last_synced_at


def test_background_reader_and_queue_status_skip_messages_before_cutoff(settings):
    _seed_account(settings, cutoff="2026-11-09T02:00:00+00:00")

    assert queue_status(settings)["queued"] == 1
    assert _claim_next_queued(settings) == "new"
    with get_session(settings) as session:
        assert session.get(GmailMessageRow, "old").processing_status == "received"
        assert session.get(GmailMessageRow, "new").processing_status == "analyzing"


def test_v12_migration_advances_cutoff_to_last_successful_sync(settings):
    latest_sync = "2026-11-09T02:00:00+00:00"
    with get_session(settings) as session:
        session.add(GmailAccountRow(
            id="mail-account", email="ops@example.test", status="connected",
            sync_after_at="2026-11-08T07:31:00+00:00", last_synced_at=latest_sync,
            created_at="2026-11-08T01:00:00+00:00", updated_at=latest_sync,
        ))
        session.query(SchemaVersionRow).filter(SchemaVersionRow.version >= 12).delete()
        session.commit()

        assert init_schema(session) == ["v12", "v13", "v14", "v15"]
        assert session.get(GmailAccountRow, "mail-account").sync_after_at == latest_sync
