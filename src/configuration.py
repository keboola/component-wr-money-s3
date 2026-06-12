import logging
from typing import Literal

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class FieldMapping(BaseModel):
    source_column: str
    destination_field: str = ""


class WriterConfiguration(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    # connection (config-level)
    # NOTE: app_id is NOT here — it is a component-level image parameter (set in the Developer
    # Portal), read in Component._build_client, and is intentionally not user-configurable.
    base_url: str
    client_id: str
    client_secret: str = Field(alias="#client_secret")
    agenda_id: str | None = None  # GUID of the target Money S3 agenda (company DB); required for writes

    # row-level
    endpoint: str | None = None
    write_mode: Literal["create", "update"] = "create"
    key_field: str = "guid"
    field_mapping: list[FieldMapping] = Field(default_factory=list)
    lines_table: str | None = None
    lines_join_key: list[str] = Field(default_factory=list)
    lines_field_mapping: list[FieldMapping] = Field(default_factory=list)
    batch_size: int = 100
    poll_timeout_s: int = (
        30  # max seconds to poll importStatus for a terminal state (0 = submit and report immediately)
    )
    poll_interval_s: int = 2
    continue_on_error: bool = False
    debug: bool = False

    def __init__(self, **data):
        nested = data.pop("line_items", None)
        if nested:
            for key in ("lines_table", "lines_join_key", "lines_field_mapping"):
                value = nested.get(key)
                if value not in (None, "", []):
                    data[key] = value
        try:
            super().__init__(**data)
        except ValidationError as e:
            msgs = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Configuration error: {', '.join(msgs)}") from None
        if self.debug:
            logging.getLogger().setLevel(logging.DEBUG)
