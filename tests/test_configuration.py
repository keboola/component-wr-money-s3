import pytest
from keboola.component.exceptions import UserException

from configuration import FieldMapping, WriterConfiguration


def _base(**over):
    data = {
        "base_url": "http://localhost:28085",
        "app_id": "A",
        "client_id": "C",
        "#client_secret": "S",
        "endpoint": "Company",
        "field_mapping": [{"source_column": "name", "destination_field": "businessName"}],
    }
    data.update(over)
    return data


def test_parses_minimal_and_aliases_secret():
    cfg = WriterConfiguration(**_base())
    assert cfg.client_secret == "S"
    assert cfg.scope == "s3api"  # default
    assert cfg.write_mode == "create"  # default
    assert cfg.key_field == "guid"  # default
    assert isinstance(cfg.field_mapping[0], FieldMapping)
    assert cfg.field_mapping[0].destination_field == "businessName"


def test_ignores_ui_metadata_field():
    cfg = WriterConfiguration(**_base(_metadata_={"api_fields": [{"field_name": "x"}]}))
    assert cfg.endpoint == "Company"


def test_missing_required_raises_userexception():
    with pytest.raises(UserException):
        WriterConfiguration(app_id="A")  # missing base_url/client_id/#client_secret
