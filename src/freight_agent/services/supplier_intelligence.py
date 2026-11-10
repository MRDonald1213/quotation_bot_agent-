"""Supplier directory backed by the one unified Supplier_Master_Database.csv."""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from ..domain.exceptions import NotFoundError, ValidationFailureError

DATA_FILE = PROJECT_ROOT / "mock_data" / "Supplier_Master_Database.csv"
DATA_DIR = DATA_FILE.parent  # kept as a directory handle for existing callers
DATA_NOTE = (
    "Supplier profiles and quotation rows come from the canonical master file. "
    "Values are not independently verified unless source evidence is recorded."
)


def _read_rows() -> list[dict[str, str]]:
    with DATA_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{key: (value or "").strip() for key, value in row.items()}
                for row in csv.DictReader(handle)]


def _record_status(row: dict[str, str]) -> str:
    if row.get("Quotation_ID"):
        return row.get("Record_Data_Status") or "Supplier quotation; not independently verified"
    return row.get("Data_Status") or "Supplier verification not recorded"


def _data() -> dict[str, Any]:
    suppliers: dict[str, dict[str, Any]] = {}
    for row in _read_rows():
        supplier_id = row.get("Supplier_ID")
        if not supplier_id:
            continue
        supplier = suppliers.setdefault(supplier_id, {
            **row, "services": [], "lanes": [], "contacts": [], "contracts": [],
            "compliance": [], "performance": [], "quotations": [],
        })
        supplier["Verification_Status"] = _record_status(supplier)
        if row.get("Quotation_ID"):
            supplier["quotations"].append(row)
            verification = _record_status(row)
            service = {
                "Service_ID": row["Quotation_ID"], "Supplier_ID": supplier_id,
                "Transport_Mode": row.get("Transport_Mode", ""),
                "Service_Type": row.get("Service_Type", ""),
                "Cargo_Types": row.get("Cargo_Type", ""),
                "Equipment_Types": row.get("Equipment_Type") or row.get("Container_Type", ""),
                "Service_Status": "Quotation record",
                "Data_Source": row.get("Data_Source", ""), "Verification_Status": verification,
            }
            if any(service.get(key) for key in ("Transport_Mode", "Service_Type", "Cargo_Types", "Equipment_Types")):
                supplier["services"].append(service)
            lane = {key: row.get(key, "") for key in (
                "Origin_Country", "Origin_City", "Origin_Port", "Destination_Country",
                "Destination_City", "Destination_Port", "Transport_Mode", "Service_Type",
            )}
            lane.update({"Lane_ID": row["Quotation_ID"], "Supplier_ID": supplier_id,
                         "Transit_Time_Min_Days": row.get("Transit_Time_Min_Days", ""),
                         "Transit_Time_Max_Days": row.get("Transit_Time_Max_Days", ""),
                         "Lane_Notes": "Lane stated in quotation record",
                         "Data_Source": row.get("Data_Source", ""), "Verification_Status": verification})
            if lane.get("Origin_City") or lane.get("Origin_Port"):
                supplier["lanes"].append(lane)
            supplier["contracts"].append({
                "Contract_ID": row["Quotation_ID"], "Service_Contract_Type": row.get("Service_Contract_Type", ""),
                "Payment_Terms": row.get("Payment_Terms") or row.get("Supplier_Default_Payment_Terms", ""),
                "Contract_Status": "Quotation terms; not a supplier contract",
                "Accepted_Currencies": row.get("Currency", ""), "Rate_Basis": row.get("Pricing_Basis", ""),
                "Fuel_Surcharge_Type": row.get("Fuel_Surcharge", ""),
                "Included_Charges": row.get("Included_Charges", ""),
                "Excluded_Charges": row.get("Excluded_Charges", ""),
                "Verification_Status": verification,
            })
        elif row.get("Primary_Contact_Email"):
            supplier["contacts"].append({"Contact_ID": supplier_id + "-primary",
                "Supplier_ID": supplier_id, "Contact_Name": "", "Email": row["Primary_Contact_Email"],
                "Phone_Number": "", "Is_Primary_Contact": "yes",
                "Verification_Status": _record_status(row), "Data_Source": row.get("Data_Source", "")})
        if row.get("Compliance_Status"):
            supplier["compliance"] = [{"Compliance_Type": row["Compliance_Status"],
                "Certification_Name": row["Compliance_Status"], "Verification_Status": _record_status(row),
                "Data_Source": row.get("Data_Source", "")}]
    return {"suppliers": suppliers}


def _with_related(record: dict[str, Any], _data: dict[str, Any]) -> dict[str, Any]:
    return dict(record)


def _exact_lane(lane: dict[str, str], origin: str, destination: str) -> bool:
    starts = {lane.get("Origin_City", "").casefold().strip(), lane.get("Origin_Port", "").casefold().strip()}
    ends = {lane.get("Destination_City", "").casefold().strip(), lane.get("Destination_Port", "").casefold().strip()}
    return origin.casefold().strip() in starts and destination.casefold().strip() in ends


def _extract_route(question: str, suppliers: dict[str, dict[str, Any]]) -> tuple[str, str]:
    locations = {value for supplier in suppliers.values() for lane in supplier["lanes"]
                 for key in ("Origin_City", "Origin_Port", "Destination_City", "Destination_Port")
                 if (value := lane.get(key, "").strip())}
    alternatives = "|".join(re.escape(value) for value in sorted(locations, key=len, reverse=True))
    if alternatives:
        match = re.search(rf"\b(?:from|between)\s+({alternatives})(?!\w)\s+(?:to|and|→)\s+({alternatives})(?!\w)",
                          question, re.I)
        if match:
            return match.group(1), match.group(2)
    clause_end = r"(?=\s+and\s+(?:tell|show|explain|compare|find|identify|recommend|let|say|give|provide)\b|[?.!,;]|$)"
    match = re.search(rf"\bfrom\s+(.+?)\s+(?:to|→)\s+(.+?){clause_end}", question, re.I)
    if match:
        return match.group(1).strip(" .,'"), match.group(2).strip(" .,'")
    return "", ""


def list_suppliers(query: str = "", service: str = "", mode: str = "", compliance: str = "",
                   category: str = "", origin: str = "", destination: str = "") -> dict[str, Any]:
    data = _data()
    terms = [part.casefold() for part in re.split(r"\s+", query.strip()) if part]
    found = []
    for supplier in data["suppliers"].values():
        item = _with_related(supplier, data)
        related = item["services"] + item["lanes"] + item["contracts"] + item["compliance"]
        searchable = " ".join([str(v) for v in supplier.values()] +
                              [str(v) for row in related for v in row.values()]).casefold()
        if terms and not all(term in searchable for term in terms):
            continue
        if category and category.casefold() not in str(item.get("Category", "")).casefold():
            continue
        if service and not any(service.casefold() in " ".join(row.values()).casefold() for row in item["services"]):
            continue
        if mode and not any(mode.casefold() in row.get("Transport_Mode", "").casefold()
                            for row in item["services"] + item["lanes"]):
            continue
        if compliance and compliance.casefold() not in str(item.get("Compliance_Status", "")).casefold():
            continue
        if origin and destination and not any(_exact_lane(lane, origin, destination) for lane in item["lanes"]):
            continue
        if origin and not destination and not any(origin.casefold() in (lane.get("Origin_City", "") + lane.get("Origin_Port", "")).casefold() for lane in item["lanes"]):
            continue
        if destination and not origin and not any(destination.casefold() in (lane.get("Destination_City", "") + lane.get("Destination_Port", "")).casefold() for lane in item["lanes"]):
            continue
        found.append(item)
    return {"suppliers": found, "count": len(found), "data_note": DATA_NOTE}


def supplier_detail(supplier_id: str) -> dict[str, Any]:
    supplier = _data()["suppliers"].get(supplier_id)
    if supplier is None:
        raise NotFoundError("supplier record not found")
    return {"supplier": supplier, "data_note": DATA_NOTE}


def query(question: str) -> dict[str, Any]:
    question = question.strip()
    if not question:
        raise ValidationFailureError("Enter a supplier or route question")
    data = _data()
    q = question.casefold()
    origin, destination = _extract_route(q, data["suppliers"])
    if any(term in q for term in ("otd", "billing accuracy", "supplier performance", "on-time rating")):
        return {"question": question, "origin": origin or None, "destination": destination or None,
                "requested_mode": None, "requested_capability": None, "requested_equipment": None,
                "supplier_comparison": [], "exact_matches": [], "partial_matches": [],
                "explanation": "OTD and billing accuracy are excluded from active quotation extraction and comparison. Compare documented quotation price, route, charges, payment terms and delivery time instead.",
                "data_note": DATA_NOTE, "ranking": None}
    mode_aliases = {"ocean": ("ocean", "sea", "maritime"), "air": ("air",),
                    "rail": ("rail", "train"), "road": ("road", "truck", "drayage")}
    requested_mode = next((mode for mode, aliases in mode_aliases.items() if any(alias in q for alias in aliases)), "")
    capability_terms = {"reefer": ("reefer", "refrigerat", "temperature-controlled", "cold chain"),
                        "customs brokerage": ("customs brokerage", "customs broker", "customs clearance"),
                        "warehousing": ("warehouse", "warehousing", "storage"),
                        "dangerous goods": ("dangerous goods", "hazmat")}
    capability = next((name for name, aliases in capability_terms.items() if any(term in q for term in aliases)), "")
    equipment_match = re.search(r"\b(20(?:gp|ft)?|40(?:gp|hc|hq|ft)?|45(?:hc|hq)|reefer)\b", q)
    requested_equipment = equipment_match.group(1).upper() if equipment_match else ""
    payment_match = re.search(r"\b(net\s*\d+|prepayment|prepaid|cash in advance)\b", q)
    requested_payment = payment_match.group(1).replace(" ", "").upper() if payment_match else ""
    named = []
    exact, partial = [], []
    for supplier in data["suppliers"].values():
        item = _with_related(supplier, data)
        name = str(item.get("Supplier_Name", ""))
        normalized_name = re.sub(r"\W+", " ", name.casefold()).strip()
        query_name = re.sub(r"\W+", " ", q).strip()
        name_match = bool(normalized_name and normalized_name in query_name)
        if name_match:
            named.append(item)
        service_text = " ".join(" ".join(row.values()) for row in item["services"]).casefold()
        mode_ok = not requested_mode or any(any(alias in row.get("Transport_Mode", "").casefold()
                                                for alias in mode_aliases[requested_mode])
                                            for row in item["services"] + item["lanes"])
        capability_ok = not capability or any(term in service_text for term in capability_terms[capability])
        equipment_ok = not requested_equipment or any(requested_equipment.casefold() in row.get("Equipment_Types", "").casefold() for row in item["services"])
        exact_lanes = [lane for lane in item["lanes"] if origin and destination and _exact_lane(lane, origin, destination)]
        route_ok = bool(exact_lanes)
        payment_ok = not requested_payment or requested_payment.casefold() in " ".join(
            row.get("Payment_Terms", "") for row in item["contracts"]).casefold().replace(" ", "")
        criteria = []
        if origin and destination:
            criteria.append(("exact_lane", route_ok, "Recorded quote lane contains both requested locations" if route_ok else "No exact route is stated in a quotation row"))
        if requested_mode:
            criteria.append(("transport_mode", mode_ok, f"{requested_mode} transport is recorded" if mode_ok else f"{requested_mode} transport is not recorded"))
        if capability:
            criteria.append(("capability", capability_ok, f"{capability} appears in a quote row" if capability_ok else f"{capability} is not recorded"))
        if requested_equipment:
            criteria.append(("equipment", equipment_ok, f"{requested_equipment} equipment is recorded" if equipment_ok else f"{requested_equipment} is not recorded"))
        if requested_payment:
            criteria.append(("payment_terms", payment_ok, f"{requested_payment} appears in quotation terms" if payment_ok else f"{requested_payment} is not recorded"))
        if criteria:
            passed = [label for label, ok, _ in criteria if ok]
            if not passed and not name_match:
                continue
            item["match_kind"] = "exact_lane" if route_ok and all(ok for _, ok, _ in criteria) else "recorded_fit" if all(ok for _, ok, _ in criteria) else "partial_fit"
            item["match_dimensions"] = passed
            item["match_reasons"] = [reason for _, _, reason in criteria]
            item["match_reason"] = "; ".join(item["match_reasons"])
            item["source_references"] = sorted({row.get("Data_Source", "") for row in item["quotations"] if row.get("Data_Source")})
            (exact if all(ok for _, ok, _ in criteria) else partial).append(item)
        elif name_match:
            item["match_kind"] = "supplier_name"
            item["match_dimensions"] = ["supplier_name"]
            item["match_reason"] = "Supplier name matches the query; quotation applicability is not implied."
            item["source_references"] = []
            exact.append(item)
    wants_compare = any(term in q for term in ("compare", "versus", " vs "))
    wants_recommendation = bool(re.search(r"\b(best|better|recommend|most suitable|optimal)\b", q))
    explanation = (f"For {origin} → {destination}, I found {len(exact)} exact and {len(partial)} partial supplier record fit(s)."
                   if origin and destination else f"I found {len(exact)} recorded fit(s) and {len(partial)} partial fit(s).")
    if wants_recommendation:
        explanation += " Supplier directory details alone are not enough to recommend a carrier. Review comparable quotation values and their source evidence before deciding."
    if exact or partial:
        explanation += " Matches are based only on fields present in this single master file."
    return {"question": question, "origin": origin or None, "destination": destination or None,
            "requested_mode": requested_mode or None, "requested_capability": capability or None,
            "requested_equipment": requested_equipment or None,
            "supplier_comparison": [{"supplier": item} for item in named[:4]] if wants_compare else [],
            "exact_matches": exact, "partial_matches": partial,
            "explanation": explanation, "data_note": DATA_NOTE, "ranking": None}


__all__ = ["list_suppliers", "supplier_detail", "query", "DATA_FILE", "DATA_DIR", "DATA_NOTE"]
