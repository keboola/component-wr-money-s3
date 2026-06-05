from configuration import FieldMapping
from mapping import build_document_payload, build_record_payload, coerce_value, fuzzy_match


def test_fuzzy_match_normalizes_and_matches():
    fields = ["businessName", "identificationNumber", "email"]
    assert fuzzy_match("business_name", fields) == "businessName"
    assert fuzzy_match("e-mail", fields) == "email"
    assert fuzzy_match("totally_unrelated_xyz", fields) == ""


def test_coerce_value_by_type():
    assert coerce_value("5", "Int") == 5
    assert coerce_value("1.50", "Decimal") == 1.5
    assert coerce_value("true", "Boolean") is True
    assert coerce_value("false", "Boolean") is False
    assert coerce_value("", "String") is None
    assert coerce_value("hello", "String") == "hello"


def test_build_record_payload_maps_and_coerces():
    row = {"name": "ACME", "vat_payer": "true", "ignored": "x"}
    fm = [
        FieldMapping(source_column="name", destination_field="businessName"),
        FieldMapping(source_column="vat_payer", destination_field="isVatPayer"),
        FieldMapping(source_column="ignored", destination_field=""),
    ]  # unmapped -> skipped
    types = {"businessName": "String", "isVatPayer": "Boolean"}
    assert build_record_payload(row, fm, types) == {"businessName": "ACME", "isVatPayer": True}


def test_build_record_payload_keeps_falsey_values():
    # False / 0 must survive the None-skip (they are valid values, not "missing").
    row = {"flag": "false", "n": "0"}
    fm = [
        FieldMapping(source_column="flag", destination_field="isActive"),
        FieldMapping(source_column="n", destination_field="count"),
    ]
    types = {"isActive": "Boolean", "count": "Int"}
    assert build_record_payload(row, fm, types) == {"isActive": False, "count": 0}


def test_build_record_payload_expands_dotted_paths():
    row = {"street": "Main 1", "city": "Brno"}
    fm = [
        FieldMapping(source_column="street", destination_field="billingAddress.street"),
        FieldMapping(source_column="city", destination_field="billingAddress.city"),
    ]
    types = {"billingAddress.street": "String", "billingAddress.city": "String"}
    assert build_record_payload(row, fm, types) == {"billingAddress": {"street": "Main 1", "city": "Brno"}}


def test_build_document_payload_nests_line_items():
    header = {"doc_no": "F1", "var_sym": "123"}
    lines = [{"doc_no": "F1", "qty": "2", "desc": "Widget"}, {"doc_no": "F1", "qty": "1", "desc": "Gadget"}]
    header_fm = [FieldMapping(source_column="var_sym", destination_field="variableSymbol")]
    line_fm = [
        FieldMapping(source_column="qty", destination_field="quantity"),
        FieldMapping(source_column="desc", destination_field="description"),
    ]
    header_types = {"variableSymbol": "String"}
    line_types = {"quantity": "Decimal", "description": "String"}
    payload = build_document_payload(header, lines, header_fm, line_fm, header_types, line_types, "items")
    assert payload["variableSymbol"] == "123"
    assert payload["items"] == [
        {"quantity": 2.0, "description": "Widget"},
        {"quantity": 1.0, "description": "Gadget"},
    ]
