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


def test_nested_line_items_is_flattened():
    # The UI nests the line fields under a collapsible "line_items" object.
    cfg = WriterConfiguration(
        **_base(
            line_items={
                "lines_table": "lines.csv",
                "lines_join_key": ["invoice_number"],
                "lines_field_mapping": [{"source_column": "qty", "destination_field": "amount"}],
            }
        )
    )
    assert cfg.lines_table == "lines.csv"
    assert cfg.lines_join_key == ["invoice_number"]
    assert cfg.lines_field_mapping[0].destination_field == "amount"


def test_flat_line_items_work():
    # Line fields may also be configured flat (not nested under "line_items").
    cfg = WriterConfiguration(**_base(lines_table="lines.csv", lines_join_key=["invoice_number"]))
    assert cfg.lines_table == "lines.csv"
    assert cfg.lines_join_key == ["invoice_number"]


def test_lines_join_key_accepts_multiple_columns():
    # Composite join key: the UI multi-picker stores a list, which is preserved as-is.
    cfg = WriterConfiguration(**_base(lines_join_key=["order_number", "branch_code"]))
    assert cfg.lines_join_key == ["order_number", "branch_code"]


def test_lines_join_key_empty_defaults_to_list():
    cfg = WriterConfiguration(**_base())
    assert cfg.lines_join_key == []
