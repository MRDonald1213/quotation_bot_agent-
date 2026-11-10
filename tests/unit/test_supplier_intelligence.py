from __future__ import annotations

import csv

from freight_agent.services.supplier_intelligence import DATA_DIR, DATA_FILE, list_suppliers, query


def test_bundled_supplier_master_contains_only_its_schema() -> None:
    assert DATA_FILE.name == "Supplier_Master_Database.csv"
    assert {path.name for path in DATA_DIR.glob("*.csv")} == {DATA_FILE.name}
    with DATA_FILE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == []


def test_empty_supplier_directory_and_route_query_do_not_invent_records() -> None:
    result = list_suppliers(category="Ocean Carrier", origin="Shenzhen", destination="Hamburg")
    assert result["suppliers"] == []
    assert result["data_note"]

    answer = query("Find ocean suppliers operating between Shenzhen and Hamburg")
    assert answer["exact_matches"] == []
    assert answer["partial_matches"] == []
    assert answer["supplier_comparison"] == []
    assert answer["ranking"] is None


def test_performance_data_is_not_ranked() -> None:
    result = query("Which suppliers have the best OTD performance?")
    assert result["ranking"] is None
    assert not result["exact_matches"] and not result["partial_matches"]
    assert "excluded from active quotation extraction and comparison" in result["explanation"]


def test_named_supplier_comparison_does_not_use_hidden_seed_rows() -> None:
    result = query("Compare Maersk Line and CMA CGM across quotations")
    assert result["supplier_comparison"] == []
    assert result["ranking"] is None


