from unittest.mock import MagicMock

import pytest
from keboola.component.exceptions import UserException

from client import MoneyS3Client, _render_type, humanize


def _resp(status, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.text = text
    return r


def _client(session):
    tm = MagicMock()
    tm.get_token.return_value = "tok"
    return MoneyS3Client("http://h", tm, session=session)


def test_execute_returns_data_and_sends_bearer():
    session = MagicMock()
    session.post.return_value = _resp(200, {"data": {"ok": 1}})
    data = _client(session).execute("{ ok }")
    assert data == {"ok": 1}
    _, kwargs = session.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
    assert kwargs["json"]["query"] == "{ ok }"


def test_execute_raises_on_graphql_errors():
    session = MagicMock()
    session.post.return_value = _resp(200, {"errors": [{"message": "boom"}]})
    with pytest.raises(UserException):
        _client(session).execute("{ ok }")


def test_render_type_and_humanize():
    t = {"kind": "NON_NULL", "name": None, "ofType": {"kind": "INPUT_OBJECT", "name": "CompanyInput", "ofType": None}}
    assert _render_type(t) == "CompanyInput!"
    assert humanize("IssuedInvoice") == "Issued Invoice"


_MUTATIONS = {
    "data": {
        "__schema": {
            "mutationType": {
                "fields": [
                    {
                        "name": "createCompany",
                        "args": [
                            {
                                "name": "company",
                                "type": {
                                    "kind": "NON_NULL",
                                    "name": None,
                                    "ofType": {"kind": "INPUT_OBJECT", "name": "CompanyInput", "ofType": None},
                                },
                            },
                            {"name": "supressWarnings", "type": {"kind": "SCALAR", "name": "Boolean", "ofType": None}},
                        ],
                    },
                    {
                        "name": "updateCompany",
                        "args": [
                            {
                                "name": "company",
                                "type": {
                                    "kind": "NON_NULL",
                                    "name": None,
                                    "ofType": {"kind": "INPUT_OBJECT", "name": "CompanyInput", "ofType": None},
                                },
                            }
                        ],
                    },
                    {"name": "deleteCompany", "args": []},
                ]
            }
        }
    }
}

_COMPANY_TYPE = {
    "data": {
        "__type": {
            "inputFields": [
                {"name": "businessName", "type": {"kind": "SCALAR", "name": "String", "ofType": None}},
                {"name": "guid", "type": {"kind": "SCALAR", "name": "UUID", "ofType": None}},
                {
                    "name": "items",
                    "type": {
                        "kind": "LIST",
                        "name": None,
                        "ofType": {"kind": "INPUT_OBJECT", "name": "ItemInput", "ofType": None},
                    },
                },
            ]
        }
    }
}


def test_list_create_endpoints_strips_prefix():
    session = MagicMock()
    session.post.return_value = _resp(200, _MUTATIONS)
    eps = _client(session).list_create_endpoints()
    assert {"value": "Company", "label": "Company"} in eps


def test_resolve_mutation_picks_main_input_arg():
    session = MagicMock()
    session.post.return_value = _resp(200, _MUTATIONS)
    name, arg, rendered = _client(session).resolve_mutation("Company", "update")
    assert (name, arg, rendered) == ("updateCompany", "company", "CompanyInput!")


def test_get_input_fields_flags_list_and_required():
    session = MagicMock()
    session.post.return_value = _resp(200, _COMPANY_TYPE)
    fields = {f["field_name"]: f for f in _client(session).get_input_fields("CompanyInput")}
    assert fields["businessName"]["type"] == "String"
    assert fields["items"]["is_list"] is True


def test_submit_returns_guid_and_builds_mutation():
    session = MagicMock()
    session.post.return_value = _resp(200, {"data": {"createCompany": {"guid": "G1", "isSuccess": True}}})
    guid = _client(session).submit("createCompany", "company", "CompanyInput!", {"businessName": "X"})
    assert guid == "G1"
    _, kwargs = session.post.call_args
    assert "mutation($input: CompanyInput!)" in kwargs["json"]["query"]
    assert "createCompany(company: $input)" in kwargs["json"]["query"]
    assert kwargs["json"]["variables"] == {"input": {"businessName": "X"}}


def test_import_status_returns_state_and_info():
    session = MagicMock()
    session.post.return_value = _resp(200, {"data": {"importStatus": {"state": "OK", "stateInfo": "done"}}})
    assert _client(session).import_status("G1") == ("OK", "done")
    # importGuid is a non-null UUID! — the variable must be declared non-null or the API rejects it.
    assert "UUID!" in session.post.call_args[1]["json"]["query"]


def test_wait_for_import_polls_until_terminal():
    session = MagicMock()
    session.post.side_effect = [
        _resp(200, {"data": {"importStatus": {"state": "IN_PROCESS", "stateInfo": ""}}}),
        _resp(200, {"data": {"importStatus": {"state": "OK", "stateInfo": "done"}}}),
    ]
    state, info = _client(session).wait_for_import("G1", timeout_s=5, interval_s=0)
    assert state == "OK"


def test_resolve_mutation_raises_when_mutation_missing():
    session = MagicMock()
    session.post.return_value = _resp(200, _MUTATIONS)
    with pytest.raises(UserException):
        _client(session).resolve_mutation("Ghost", "create")


def test_resolve_mutation_raises_when_no_input_arg():
    schema = {
        "data": {
            "__schema": {
                "mutationType": {
                    "fields": [
                        {
                            "name": "createThing",
                            "args": [{"name": "flag", "type": {"kind": "SCALAR", "name": "Boolean", "ofType": None}}],
                        },
                    ]
                }
            }
        }
    }
    session = MagicMock()
    session.post.return_value = _resp(200, schema)
    with pytest.raises(UserException):
        _client(session).resolve_mutation("Thing", "create")


def test_submit_raises_when_no_guid_returned():
    session = MagicMock()
    session.post.return_value = _resp(200, {"data": {"createCompany": {}}})
    with pytest.raises(UserException):
        _client(session).submit("createCompany", "company", "CompanyInput!", {"businessName": "X"})


def test_wait_for_import_returns_non_terminal_on_timeout():
    session = MagicMock()
    session.post.return_value = _resp(200, {"data": {"importStatus": {"state": "IN_PROCESS", "stateInfo": "queued"}}})
    state, info = _client(session).wait_for_import("G1", timeout_s=0, interval_s=0)
    assert state == "IN_PROCESS"
    assert info == "queued"


def test_execute_sends_agenda_id_header_when_set():
    session = MagicMock()
    session.post.return_value = _resp(200, {"data": {"ok": 1}})
    tm = MagicMock()
    tm.get_token.return_value = "tok"
    MoneyS3Client("http://h", tm, session=session, agenda_id="AG-9").execute("{ ok }")
    _, kwargs = session.post.call_args
    assert kwargs["headers"]["AgendaId"] == "AG-9"


def test_execute_omits_agenda_id_header_when_unset():
    session = MagicMock()
    session.post.return_value = _resp(200, {"data": {"ok": 1}})
    _client(session).execute("{ ok }")  # _client builds with agenda_id=None
    _, kwargs = session.post.call_args
    assert "AgendaId" not in kwargs["headers"]


def test_list_agendas_parses_items():
    session = MagicMock()
    session.post.return_value = _resp(
        200,
        {
            "data": {
                "agendas": {
                    "items": [{"guid": "g1", "displayName": "Agenda One"}, {"guid": "g2", "displayName": "Agenda Two"}]
                }
            }
        },
    )
    assert _client(session).list_agendas() == [
        {"value": "g1", "label": "Agenda One"},
        {"value": "g2", "label": "Agenda Two"},
    ]
