from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from freight_agent.domain.exceptions import NotFoundError
from freight_agent.services import intake as intake_service
from freight_agent.services.common import with_session
from freight_agent.services.shipments import create_shipment
from sqlalchemy import text

from freight_agent.db.engine import get_session


def test_foreign_keys_enforced(settings, quote_a_file):
    session = get_session(settings)
    try:
        with pytest.raises(IntegrityError):
            session.execute(text(
                "INSERT INTO quotations (id, shipment_id, intake_item_id, charges_json, "
                "included_services_json, excluded_services_json, assumptions_json, "
                "comparison_status, created_at, updated_at, version) "
                "VALUES ('q1','does-not-exist','i1','[]','[]','[]','[]','comparable','x','y',1)"))
    finally:
        session.close()


def test_shipment_reference_unique(settings):
    create_shipment(settings, reference="R-1", name="a", origin="o", destination="d",
                    cargo_description="c")
    from freight_agent.domain.exceptions import DatabaseError, ValidationFailureError
    with pytest.raises((IntegrityError, DatabaseError, ValidationFailureError)):
        create_shipment(settings, reference="R-1", name="b", origin="o", destination="d",
                        cargo_description="c")


def test_get_shipment_by_reference_or_id(settings):
    s = create_shipment(settings, reference="R-2", name="a", origin="o", destination="d",
                        cargo_description="c")
    with with_session(settings) as repos:
        assert repos.get_shipment("R-2").id == s.id
        assert repos.get_shipment(str(s.id)).reference == "R-2"
        with pytest.raises(NotFoundError):
            repos.get_shipment("nope")
