import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from keboola.component.exceptions import UserException

from component import Component


def _datadir(tmp_path: Path, params: dict, action: str) -> Path:
    data = tmp_path / "data"
    (data / "in" / "tables").mkdir(parents=True)
    with open(data / "config.json", "w") as fh:
        json.dump({"parameters": params, "action": action, "storage": {}}, fh)
    return data


def _params(**over):
    p = {"base_url": "http://h", "app_id": "A", "client_id": "C", "#client_secret": "S"}
    p.update(over)
    return p


def test_test_connection_ok(tmp_path, monkeypatch):
    data = _datadir(tmp_path, _params(), "run")
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.list_create_endpoints.return_value = [{"value": "Company", "label": "Company"}]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg: fake)
    assert comp.test_connection() is None


def test_test_connection_failure_raises(tmp_path, monkeypatch):
    data = _datadir(tmp_path, _params(), "run")
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.list_create_endpoints.side_effect = UserException("bad creds")
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg: fake)
    with pytest.raises(UserException):
        comp.test_connection()


def test_list_endpoints_returns_select_elements(tmp_path, monkeypatch):
    data = _datadir(tmp_path, _params(), "run")
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.list_create_endpoints.return_value = [
        {"value": "Company", "label": "Company"},
        {"value": "IssuedInvoice", "label": "Issued Invoice"},
    ]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg: fake)
    res = comp.list_endpoints()
    payload = json.loads(f"[{', '.join(str(r) for r in res)}]")
    assert {"value": "Company", "label": "Company"} in payload


def test_list_agendas_returns_select_elements(tmp_path, monkeypatch):
    data = _datadir(tmp_path, _params(), "run")
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.list_agendas.return_value = [{"value": "g1", "label": "Agenda One"}]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg: fake)
    res = comp.list_agendas()
    payload = json.loads(f"[{', '.join(str(r) for r in res)}]")
    assert {"value": "g1", "label": "Agenda One"} in payload


def test_load_field_mapping_fuzzy_fills_and_preserves(tmp_path, monkeypatch):
    params = _params(endpoint="Company", field_mapping=[{"source_column": "vat_payer", "destination_field": "note"}])
    data = _datadir(tmp_path, params, "run")
    cfg = json.load(open(data / "config.json"))
    cfg["storage"] = {
        "input": {
            "tables": [
                {
                    "source": "in.c-main.partners",
                    "destination": "partners.csv",
                    "columns": ["business_name", "vat_payer", "zzz_unrelated"],
                }
            ]
        }
    }
    json.dump(cfg, open(data / "config.json", "w"))
    monkeypatch.setenv("KBC_DATADIR", str(data))

    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createCompany", "company", "CompanyInput!")
    fake.get_input_fields.return_value = [
        {"field_name": "businessName", "label": "Business Name", "type": "String", "is_list": False},
        {"field_name": "isVatPayer", "label": "Is Vat Payer", "type": "Boolean", "is_list": False},
    ]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg_: fake)

    res = comp.load_field_mapping()
    assert res["type"] == "data"
    fm = {m["source_column"]: m["destination_field"] for m in res["data"]["field_mapping"]}
    assert fm["vat_payer"] == "note"  # user's existing mapping preserved, NOT re-fuzzed to isVatPayer
    assert fm["business_name"] == "businessName"  # fuzzy-matched new column (normalized exact match)
    assert fm["zzz_unrelated"] == ""  # no confident match -> left blank for manual mapping
    assert res["data"]["_metadata_"]["api_fields"][0]["field_name"] == "businessName"
    assert res["data"]["_metadata_"]["api_fields"][0]["label"] == "businessName"  # label = raw field name
    assert res["data"]["_metadata_"]["line_api_fields"] == []  # flat endpoint -> no line-item fields
    assert "line_items" not in res["data"]  # no lines table -> nothing to prefill


def test_load_field_mapping_document_prefills_lines(tmp_path, monkeypatch):
    # Document mode: two input tables (header + lines). Both mappings get prefilled and the line-item
    # dropdown is fed from the line element type's fields.
    params = _params(endpoint="IssuedInvoice", lines_table="lines.csv")
    data = _datadir(tmp_path, params, "run")
    cfg = json.load(open(data / "config.json"))
    cfg["storage"] = {
        "input": {
            "tables": [
                {"destination": "invoices.csv", "columns": ["document_number", "partner"]},
                {"destination": "lines.csv", "columns": ["description", "qty"]},
            ]
        }
    }
    json.dump(cfg, open(data / "config.json", "w"))
    monkeypatch.setenv("KBC_DATADIR", str(data))

    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createIssuedInvoice", "issuedInvoice", "IssuedInvoiceInput!")
    fake.get_input_fields.side_effect = [
        [  # header type: a scalar field + the line-item collection
            {"field_name": "documentNumber", "type": "String", "is_list": False},
            {"field_name": "items", "type": "IssuedInvoiceItemInput", "is_list": True},
        ],
        [  # line element type
            {"field_name": "description", "type": "String", "is_list": False},
            {"field_name": "amount", "type": "Decimal", "is_list": False},
        ],
    ]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg_: fake)
    res = comp.load_field_mapping()

    header_fm = {m["source_column"]: m["destination_field"] for m in res["data"]["field_mapping"]}
    assert header_fm["document_number"] == "documentNumber"  # header column fuzzy-matched
    assert {"field_name": "description", "label": "description"} in res["data"]["_metadata_"]["line_api_fields"]
    line_fm = {m["source_column"]: m["destination_field"] for m in res["data"]["line_items"]["lines_field_mapping"]}
    assert line_fm["description"] == "description"  # line column matched against line element fields
    assert line_fm["qty"] == ""  # no confident match -> left blank for manual mapping
    assert res["data"]["line_items"]["lines_table"] == "lines.csv"  # echoed back, not clobbered


def test_load_field_mapping_update_mode_falls_back_to_create(tmp_path, monkeypatch):
    # Most Money S3 endpoints expose only create<Endpoint>; "Re-load mapping" must still work in
    # Update mode by falling back to the create mutation's field set.
    params = _params(endpoint="Company", write_mode="update")
    data = _datadir(tmp_path, params, "run")
    cfg = json.load(open(data / "config.json"))
    cfg["storage"] = {"input": {"tables": [{"destination": "partners.csv", "columns": ["email"]}]}}
    json.dump(cfg, open(data / "config.json", "w"))
    monkeypatch.setenv("KBC_DATADIR", str(data))

    def resolve(endpoint, write_mode):
        if write_mode == "update":
            raise UserException("Money S3 has no 'updateCompany' mutation for endpoint 'Company'.")
        return ("createCompany", "company", "CompanyInput!")

    fake = MagicMock()
    fake.resolve_mutation.side_effect = resolve
    fake.get_input_fields.return_value = [
        {"field_name": "email", "type": "String", "is_list": False},
    ]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg_: fake)
    res = comp.load_field_mapping()
    assert {"field_name": "email", "label": "email"} in res["data"]["_metadata_"]["api_fields"]
