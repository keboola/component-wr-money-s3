import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from keboola.component.exceptions import UserException

from component import Component


def _make_datadir(tmp_path: Path, params: dict, table_rows: list[dict]) -> Path:
    data = tmp_path / "data"
    (data / "in" / "tables").mkdir(parents=True)
    (data / "out" / "tables").mkdir(parents=True)
    cols = list(table_rows[0].keys())
    src = data / "in" / "tables" / "partners.csv"
    with open(src, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(table_rows)
    with open(str(src) + ".manifest", "w") as fh:
        json.dump({"destination": "partners.csv", "columns": cols}, fh)
    cfg = {
        "parameters": params,
        "storage": {"input": {"tables": [{"source": "in.c-main.partners", "destination": "partners.csv"}]}},
        "action": "run",
    }
    with open(data / "config.json", "w") as fh:
        json.dump(cfg, fh)
    return data


def _base_params(**over):
    p = {
        "base_url": "http://h",
        "app_id": "A",
        "client_id": "C",
        "#client_secret": "S",
        "agenda_id": "AG-1",
        "endpoint": "Company",
        "field_mapping": [{"source_column": "name", "destination_field": "businessName"}],
    }
    p.update(over)
    return p


def _setup(tmp_path, monkeypatch, params, rows, fake):
    data = _make_datadir(tmp_path, params, rows)
    monkeypatch.setenv("KBC_DATADIR", str(data))
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda cfg: fake)
    return comp, data


def _fake_company(submit=None, wait=None):
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createCompany", "company", "CompanyInput!")
    fake.get_input_fields.return_value = [{"field_name": "businessName", "type": "String", "is_list": False}]
    if submit is not None:
        fake.submit.side_effect = submit if isinstance(submit, list) else None
        if not isinstance(submit, list):
            fake.submit.return_value = submit
    if wait is not None:
        fake.wait_for_import.side_effect = wait if isinstance(wait, list) else None
        if not isinstance(wait, list):
            fake.wait_for_import.return_value = wait
    return fake


def test_run_submits_each_row_and_writes_results(tmp_path, monkeypatch):
    fake = _fake_company(submit=["G1", "G2"], wait=[("OK", ""), ("OK", "")])
    comp, data = _setup(tmp_path, monkeypatch, _base_params(), [{"name": "ACME"}, {"name": "Globex"}], fake)
    comp.run()
    assert fake.submit.call_count == 2
    out = data / "out" / "tables" / "write_log.csv"
    with open(out) as fh:
        rows = list(csv.DictReader(fh))
    assert [r["import_guid"] for r in rows] == ["G1", "G2"]
    assert all(r["state"] == "OK" for r in rows)


def test_error_state_fails_job_when_not_continue(tmp_path, monkeypatch):
    fake = _fake_company(submit="G1", wait=("ERROR", "insufficient data"))
    comp, _ = _setup(tmp_path, monkeypatch, _base_params(continue_on_error=False), [{"name": "ACME"}], fake)
    with pytest.raises(UserException):
        comp.run()


def test_error_state_recorded_when_continue(tmp_path, monkeypatch):
    fake = _fake_company(submit="G1", wait=("ERROR", "insufficient data"))
    comp, data = _setup(tmp_path, monkeypatch, _base_params(continue_on_error=True), [{"name": "ACME"}], fake)
    comp.run()  # must NOT raise
    out = data / "out" / "tables" / "write_log.csv"
    rows = list(csv.DictReader(open(out)))
    assert rows[0]["state"] == "ERROR" and rows[0]["state_info"] == "insufficient data"


def test_coercion_error_raises_userexception(tmp_path, monkeypatch):
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createCompany", "company", "CompanyInput!")
    fake.get_input_fields.return_value = [{"field_name": "count", "type": "Int", "is_list": False}]
    comp, _ = _setup(
        tmp_path,
        monkeypatch,
        _base_params(field_mapping=[{"source_column": "n", "destination_field": "count"}]),
        [{"n": "not-a-number"}],
        fake,
    )
    with pytest.raises(UserException):
        comp.run()


def test_coercion_error_fails_fast_even_when_continue_on_error(tmp_path, monkeypatch):
    # A coercion failure is structural (wrong type/mapping), so it must abort even with
    # continue_on_error=True, which only governs per-record server-side import errors.
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createCompany", "company", "CompanyInput!")
    fake.get_input_fields.return_value = [{"field_name": "count", "type": "Int", "is_list": False}]
    comp, _ = _setup(
        tmp_path,
        monkeypatch,
        _base_params(continue_on_error=True, field_mapping=[{"source_column": "n", "destination_field": "count"}]),
        [{"n": "not-a-number"}],
        fake,
    )
    with pytest.raises(UserException):
        comp.run()


def test_missing_endpoint_raises(tmp_path, monkeypatch):
    comp, _ = _setup(tmp_path, monkeypatch, _base_params(endpoint=None), [{"name": "ACME"}], _fake_company())
    with pytest.raises(UserException):
        comp.run()


def test_run_missing_agenda_raises(tmp_path, monkeypatch):
    # endpoint set but no agenda configured -> writes require an agenda -> UserException (exit 1)
    comp, _ = _setup(tmp_path, monkeypatch, _base_params(agenda_id=None), [{"name": "ACME"}], _fake_company())
    with pytest.raises(UserException):
        comp.run()


def test_requires_exactly_one_input_table(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "in" / "tables").mkdir(parents=True)
    (data / "out" / "tables").mkdir(parents=True)
    for name in ("a.csv", "b.csv"):
        p = data / "in" / "tables" / name
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["name"])
            w.writeheader()
            w.writerow({"name": "X"})
        with open(str(p) + ".manifest", "w") as fh:
            json.dump({"destination": name, "columns": ["name"]}, fh)
    cfg = {
        "parameters": _base_params(),
        "action": "run",
        "storage": {
            "input": {
                "tables": [
                    {"source": "in.c-main.a", "destination": "a.csv"},
                    {"source": "in.c-main.b", "destination": "b.csv"},
                ]
            }
        },
    }
    with open(data / "config.json", "w") as fh:
        json.dump(cfg, fh)
    monkeypatch.setenv("KBC_DATADIR", str(data))
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda c: _fake_company())
    with pytest.raises(UserException):
        comp.run()


def test_key_value_captured_in_results(tmp_path, monkeypatch):
    fake = _fake_company(submit="G1", wait=("OK", ""))
    comp, data = _setup(tmp_path, monkeypatch, _base_params(key_field="name"), [{"name": "ACME"}], fake)
    comp.run()
    rows = list(csv.DictReader(open(data / "out" / "tables" / "write_log.csv")))
    assert rows[0]["key_value"] == "ACME"


def _make_doc_datadir(tmp_path, params, header_rows, line_rows):
    data = tmp_path / "data"
    (data / "in" / "tables").mkdir(parents=True)
    (data / "out" / "tables").mkdir(parents=True)
    for fname, rows in [("invoices.csv", header_rows), ("lines.csv", line_rows)]:
        cols = list(rows[0].keys())
        p = data / "in" / "tables" / fname
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        with open(str(p) + ".manifest", "w") as fh:
            json.dump({"destination": fname, "columns": cols}, fh)
    cfg = {
        "parameters": params,
        "action": "run",
        "storage": {
            "input": {
                "tables": [
                    {"source": "in.c-main.invoices", "destination": "invoices.csv"},
                    {"source": "in.c-main.lines", "destination": "lines.csv"},
                ]
            }
        },
    }
    with open(data / "config.json", "w") as fh:
        json.dump(cfg, fh)
    return data


def test_run_document_builds_items(tmp_path, monkeypatch):
    params = {
        "base_url": "http://h",
        "app_id": "A",
        "client_id": "C",
        "#client_secret": "S",
        "agenda_id": "AG-1",
        "endpoint": "IssuedInvoice",
        "field_mapping": [{"source_column": "var_sym", "destination_field": "variableSymbol"}],
        "lines_table": "lines.csv",
        "lines_join_key": ["doc_no"],
        "lines_field_mapping": [{"source_column": "desc", "destination_field": "description"}],
    }
    data = _make_doc_datadir(
        tmp_path,
        params,
        [{"doc_no": "F1", "var_sym": "123"}],
        [{"doc_no": "F1", "desc": "Widget"}, {"doc_no": "F1", "desc": "Gadget"}],
    )
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createIssuedInvoice", "issuedInvoice", "IssuedInvoiceInput!")
    fake.get_input_fields.side_effect = [
        [
            {"field_name": "variableSymbol", "type": "String", "is_list": False},
            {"field_name": "items", "type": "IssuedInvoiceItemInput", "is_list": True},
        ],  # header type
        [{"field_name": "description", "type": "String", "is_list": False}],  # line element type
    ]
    fake.submit.return_value = "G1"
    fake.wait_for_import.return_value = ("OK", "")
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda c: fake)
    comp.run()
    payload = fake.submit.call_args[0][3]  # submit(mutation_name, arg_name, input_type, payload)
    assert payload["variableSymbol"] == "123"
    assert payload["items"] == [{"description": "Widget"}, {"description": "Gadget"}]


def test_run_document_composite_join_key(tmp_path, monkeypatch):
    # A two-column join key groups lines by the (branch, doc_no) tuple. Both headers share doc_no
    # "F1" and would collide on a single key -- the composite key must keep their lines separate.
    params = {
        "base_url": "http://h",
        "app_id": "A",
        "client_id": "C",
        "#client_secret": "S",
        "agenda_id": "AG-1",
        "endpoint": "IssuedInvoice",
        "field_mapping": [{"source_column": "var_sym", "destination_field": "variableSymbol"}],
        "lines_table": "lines.csv",
        "lines_join_key": ["branch", "doc_no"],
        "lines_field_mapping": [{"source_column": "desc", "destination_field": "description"}],
    }
    data = _make_doc_datadir(
        tmp_path,
        params,
        [{"branch": "A", "doc_no": "F1", "var_sym": "1"}, {"branch": "B", "doc_no": "F1", "var_sym": "2"}],
        [{"branch": "A", "doc_no": "F1", "desc": "Widget"}, {"branch": "B", "doc_no": "F1", "desc": "Gadget"}],
    )
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createIssuedInvoice", "issuedInvoice", "IssuedInvoiceInput!")
    fake.get_input_fields.side_effect = [
        [
            {"field_name": "variableSymbol", "type": "String", "is_list": False},
            {"field_name": "items", "type": "IssuedInvoiceItemInput", "is_list": True},
        ],  # header type
        [{"field_name": "description", "type": "String", "is_list": False}],  # line element type
    ]
    fake.submit.return_value = "G1"
    fake.wait_for_import.return_value = ("OK", "")
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda c: fake)
    comp.run()
    payloads = [call[0][3] for call in fake.submit.call_args_list]
    assert payloads[0]["items"] == [{"description": "Widget"}]  # branch A
    assert payloads[1]["items"] == [{"description": "Gadget"}]  # branch B -- not cross-contaminated


def test_document_picks_items_over_other_lists(tmp_path, monkeypatch):
    # IssuedInvoiceInput exposes several list-of-*Input fields (vatRateSummary, ...) BEFORE `items`;
    # line rows must land in `items`, not the first list-of-*Input encountered.
    params = {
        "base_url": "http://h",
        "app_id": "A",
        "client_id": "C",
        "#client_secret": "S",
        "agenda_id": "AG-1",
        "endpoint": "IssuedInvoice",
        "field_mapping": [{"source_column": "var_sym", "destination_field": "variableSymbol"}],
        "lines_table": "lines.csv",
        "lines_join_key": ["doc_no"],
        "lines_field_mapping": [{"source_column": "desc", "destination_field": "description"}],
    }
    data = _make_doc_datadir(
        tmp_path,
        params,
        [{"doc_no": "F1", "var_sym": "123"}],
        [{"doc_no": "F1", "desc": "Widget"}, {"doc_no": "F1", "desc": "Gadget"}],
    )
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createIssuedInvoice", "issuedInvoice", "IssuedInvoiceInput!")
    fake.get_input_fields.side_effect = [
        [
            {"field_name": "vatRateSummary", "type": "VatRateSummaryInput", "is_list": True},
            {"field_name": "variableSymbol", "type": "String", "is_list": False},
            {"field_name": "items", "type": "IssuedInvoiceItemInput", "is_list": True},
        ],  # header type: a list-of-*Input precedes items
        [{"field_name": "description", "type": "String", "is_list": False}],  # line element type
    ]
    fake.submit.return_value = "G1"
    fake.wait_for_import.return_value = ("OK", "")
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda c: fake)
    comp.run()
    payload = fake.submit.call_args[0][3]
    assert payload["items"] == [{"description": "Widget"}, {"description": "Gadget"}]
    assert "vatRateSummary" not in payload


def test_document_missing_line_item_field_raises(tmp_path, monkeypatch):
    params = {
        "base_url": "http://h",
        "app_id": "A",
        "client_id": "C",
        "#client_secret": "S",
        "agenda_id": "AG-1",
        "endpoint": "IssuedInvoice",
        "field_mapping": [],
        "lines_table": "lines.csv",
        "lines_join_key": ["doc_no"],
        "lines_field_mapping": [],
    }
    data = _make_doc_datadir(tmp_path, params, [{"doc_no": "F1"}], [{"doc_no": "F1", "desc": "X"}])
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createIssuedInvoice", "issuedInvoice", "IssuedInvoiceInput!")
    # header type has no is_list "...Input" field -> no line-item collection
    fake.get_input_fields.return_value = [{"field_name": "variableSymbol", "type": "String", "is_list": False}]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda c: fake)
    with pytest.raises(UserException):
        comp.run()


def test_document_coercion_error_raises(tmp_path, monkeypatch):
    params = {
        "base_url": "http://h",
        "app_id": "A",
        "client_id": "C",
        "#client_secret": "S",
        "agenda_id": "AG-1",
        "endpoint": "IssuedInvoice",
        "field_mapping": [],
        "lines_table": "lines.csv",
        "lines_join_key": ["doc_no"],
        "lines_field_mapping": [{"source_column": "amt", "destination_field": "unitPrice"}],
    }
    data = _make_doc_datadir(tmp_path, params, [{"doc_no": "F1"}], [{"doc_no": "F1", "amt": "not-a-number"}])
    monkeypatch.setenv("KBC_DATADIR", str(data))
    fake = MagicMock()
    fake.resolve_mutation.return_value = ("createIssuedInvoice", "issuedInvoice", "IssuedInvoiceInput!")
    fake.get_input_fields.side_effect = [
        [{"field_name": "items", "type": "IssuedInvoiceItemInput", "is_list": True}],  # header type
        [{"field_name": "unitPrice", "type": "Decimal", "is_list": False}],  # line element type
    ]
    comp = Component()
    monkeypatch.setattr(comp, "_build_client", lambda c: fake)
    with pytest.raises(UserException):
        comp.run()


def test_submit_failure_records_partial_audit(tmp_path, monkeypatch):
    # Row 0 submits OK; row 1's transport call fails. The already-submitted row must still appear in
    # write_log (write_always audit), and the job fails because continue_on_error is False.
    fake = _fake_company(submit=["G1", UserException("502 Bad Gateway")], wait=("OK", ""))
    comp, data = _setup(
        tmp_path, monkeypatch, _base_params(continue_on_error=False), [{"name": "ACME"}, {"name": "Globex"}], fake
    )
    with pytest.raises(UserException):
        comp.run()
    rows = list(csv.DictReader(open(data / "out" / "tables" / "write_log.csv")))
    assert len(rows) == 2
    assert rows[0]["import_guid"] == "G1" and rows[0]["state"] == "OK"
    assert rows[1]["state"] == "SUBMIT_FAILED" and "502" in rows[1]["state_info"]


def test_submit_failure_continues_when_continue_on_error(tmp_path, monkeypatch):
    fake = _fake_company(submit=[UserException("502"), "G2"], wait=("OK", ""))
    comp, data = _setup(
        tmp_path, monkeypatch, _base_params(continue_on_error=True), [{"name": "ACME"}, {"name": "Globex"}], fake
    )
    comp.run()  # must NOT raise
    rows = list(csv.DictReader(open(data / "out" / "tables" / "write_log.csv")))
    assert {r["state"] for r in rows} == {"SUBMIT_FAILED", "OK"}
