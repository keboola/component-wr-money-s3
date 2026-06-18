import csv
import logging
from datetime import UTC, datetime

from keboola.component.base import ComponentBase, sync_action
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement

from auth import TokenManager
from client import MoneyS3Client
from configuration import WriterConfiguration
from mapping import build_document_payload, build_record_payload, fuzzy_match

# keboola.vcr is a record-time dependency the datadirtest scaffolder reads to scrub secrets from
# cassettes; it is not used at runtime and may be absent in production, so the import is guarded.
try:
    from keboola.vcr import DefaultSanitizer

    VCR_SANITIZERS = [DefaultSanitizer(additional_sensitive_fields=["app_id", "AppId"])]
except ImportError:
    VCR_SANITIZERS = []

RESULT_COLUMNS = [
    "row_index",
    "endpoint",
    "write_mode",
    "key_value",
    "submitted_at",
    "import_guid",
    "state",
    "state_info",
]


class Component(ComponentBase):
    def __init__(self):
        super().__init__()
        self.config = WriterConfiguration(**self.configuration.parameters)
        self._client: MoneyS3Client | None = None

    @property
    def client(self) -> MoneyS3Client:
        if self._client is None:
            self._client = self._build_client(self.config)
        return self._client

    def _build_client(self, cfg: WriterConfiguration) -> MoneyS3Client:
        # app_id identifies the Money S3 application registration; it is the same for every
        # configuration of this writer, so it is provisioned as a component-level image parameter
        # (Developer Portal) rather than asked of the user. All client construction funnels through
        # here, so this is the single place that needs to read it.
        app_id = (self.configuration.image_parameters or {}).get("app_id")
        if not app_id:
            raise UserException(
                "Money S3 'app_id' is not set in the component image parameters. It is configured "
                "at the component level, not in this configuration; contact your Keboola administrator."
            )
        token_mgr = TokenManager(cfg.base_url, app_id, cfg.client_id, cfg.client_secret)
        return MoneyS3Client(cfg.base_url, token_mgr, agenda_id=cfg.agenda_id)

    def _read_rows(self, destination: str) -> list[dict]:
        for table in self.get_input_tables_definitions():
            if table.name == destination or table.full_path.endswith(destination):
                with open(table.full_path, encoding="utf-8") as fh:
                    return list(csv.DictReader(fh))
        raise UserException(f"Input table '{destination}' not found in input mapping.")

    def run(self):
        cfg = self.config
        if not cfg.endpoint:
            raise UserException("No 'endpoint' selected for this row.")
        if not cfg.agenda_id:
            raise UserException(
                "No agenda configured. Money S3 writes require a target agenda — set 'agenda_id' "
                "in the connection config (use the 'Re-load agendas' action to pick one)."
            )

        client = self.client
        mutation_name, arg_name, input_type = client.resolve_mutation(cfg.endpoint, cfg.write_mode)
        header_fields = client.get_input_fields(input_type.rstrip("!"))
        field_types = {f["field_name"]: f["type"] for f in header_fields}

        if cfg.lines_table:
            results, failed = self._run_document(
                client, cfg, mutation_name, arg_name, input_type, header_fields, field_types
            )
        else:
            tables = self.configuration.tables_input_mapping
            if len(tables) != 1:
                raise UserException(f"Exactly one input table must be mapped for this row. Found: {len(tables)}.")
            jobs = []
            for idx, row in enumerate(self._read_rows(tables[0].destination)):
                try:
                    payload = build_record_payload(row, cfg.field_mapping, field_types)
                except (ValueError, TypeError) as e:
                    # A coercion failure is a structural mapping/config error (e.g. a non-numeric column
                    # mapped to an Int field) that affects the whole table, so we fail fast regardless of
                    # continue_on_error -- which governs per-record server-side import (ImportState.ERROR).
                    raise UserException(
                        f"Row {idx}: could not convert a value for endpoint '{cfg.endpoint}': {e}"
                    ) from e
                jobs.append((idx, row, payload))
            results, failed = self._submit_in_batches(client, cfg, mutation_name, arg_name, input_type, jobs)

        self._write_results(results)
        unconfirmed = sum(1 for r in results if r["state"] in ("UNPROCESSED", "IN_PROCESS", "UNKNOWN"))
        logging.info(
            "Submitted %d record(s) to Money S3 endpoint '%s'; %d failed, %d unconfirmed (import still "
            "non-terminal within the poll window — see write_log for final state).",
            len(results),
            cfg.endpoint,
            len(failed),
            unconfirmed,
        )
        if failed and not cfg.continue_on_error:
            raise UserException(
                f"{len(failed)} record(s) failed to import (first error: {failed[0]['state_info']}). "
                f"See the write_log output table; enable 'Continue on Error' to keep going."
            )

    @staticmethod
    def _line_item_field(header_fields):
        """Return (field_name, element_input_type) for the document's line-item array, or (None, None).

        A document input type can expose several list-of-*Input fields (e.g. IssuedInvoiceInput lists
        vatRateSummary, paymentMeans, ... before items). Prefer the conventional line-item collection:
        a field named "items", then any whose element type ends in "ItemInput", before falling back to
        the first list-of-*Input.
        """
        lists = [
            (f["field_name"], f["type"])
            for f in header_fields
            if f["is_list"] and f["type"] and f["type"].endswith("Input")
        ]
        for name, typ in lists:
            if name == "items":
                return name, typ
        for name, typ in lists:
            if typ.endswith("ItemInput"):
                return name, typ
        return lists[0] if lists else (None, None)

    def _run_document(self, client, cfg, mutation_name, arg_name, input_type, header_fields, header_types):
        if not cfg.lines_join_key:
            raise UserException("A 'lines join key' is required when a lines table is configured.")
        items_field, element_type = self._line_item_field(header_fields)
        if not items_field:
            raise UserException(f"Endpoint '{cfg.endpoint}' has no line-item collection; clear the lines table.")
        line_types = {f["field_name"]: f["type"] for f in client.get_input_fields(element_type)}

        header_dest = next(
            (t.destination for t in self.configuration.tables_input_mapping if t.destination != cfg.lines_table), None
        )
        if not header_dest:
            raise UserException("Document mode needs two input tables: a header table and a lines table.")
        headers = self._read_rows(header_dest)
        all_lines = self._read_rows(cfg.lines_table)

        def join_value(row):
            return tuple(row.get(k, "") for k in cfg.lines_join_key)

        lines_by_key: dict[tuple, list] = {}
        for ln in all_lines:
            lines_by_key.setdefault(join_value(ln), []).append(ln)

        jobs = []
        for idx, header in enumerate(headers):
            key = join_value(header)
            try:
                payload = build_document_payload(
                    header,
                    lines_by_key.get(key, []),
                    cfg.field_mapping,
                    cfg.lines_field_mapping,
                    header_types,
                    line_types,
                    items_field,
                )
            except (ValueError, TypeError) as e:
                raise UserException(
                    f"Document row {idx}: could not convert a value for endpoint '{cfg.endpoint}': {e}"
                ) from e
            jobs.append((idx, header, payload))
        return self._submit_in_batches(client, cfg, mutation_name, arg_name, input_type, jobs)

    def _submit_in_batches(self, client, cfg, mutation_name, arg_name, input_type, jobs):
        """Submit each batch of up to cfg.batch_size records, then poll that batch's import statuses,
        so the agenda's async imports overlap instead of being confirmed strictly one record at a time.
        """
        size = max(cfg.batch_size, 1)
        results = []
        for start in range(0, len(jobs), size):
            submitted = []  # (base, guid, submit_error) for the batch, in row order
            for idx, row, payload in jobs[start : start + size]:
                base = {
                    "row_index": idx,
                    "endpoint": cfg.endpoint,
                    "write_mode": cfg.write_mode,
                    "key_value": row.get(cfg.key_field, ""),
                    "submitted_at": datetime.now(UTC).isoformat(),
                }
                try:
                    submitted.append((base, client.submit(mutation_name, arg_name, input_type, payload), None))
                except UserException as e:
                    # Transport/GraphQL failure: record it so the write_always audit table keeps every
                    # attempted row (rows already submitted are not lost). Gated like ERROR below.
                    submitted.append((base, None, str(e)))
            for base, guid, submit_error in submitted:
                if submit_error is not None:
                    results.append({**base, "import_guid": "", "state": "SUBMIT_FAILED", "state_info": submit_error})
                    continue
                try:
                    state, info = client.wait_for_import(
                        guid, timeout_s=cfg.poll_timeout_s, interval_s=cfg.poll_interval_s
                    )
                    results.append({**base, "import_guid": guid, "state": state or "UNKNOWN", "state_info": info or ""})
                except UserException as e:
                    results.append({**base, "import_guid": guid, "state": "SUBMIT_FAILED", "state_info": str(e)})
        failed = [r for r in results if r["state"] in ("ERROR", "SUBMIT_FAILED")]
        return results, failed

    def _write_results(self, results: list[dict]):
        # Append-only audit log: every run's attempts accumulate (write_always keeps them even on a
        # failed job). No primary key -- creates are not idempotent (each run yields fresh GUIDs) and
        # failed-before-submit rows have no GUID, so upsert-by-GUID would be unsound and collide.
        out = self.create_out_table_definition(
            "write_log.csv",
            schema=RESULT_COLUMNS,
            incremental=True,
            write_always=True,
            has_header=True,
        )
        with open(out.full_path, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=RESULT_COLUMNS)
            w.writeheader()
            w.writerows(results)
        self.write_manifest(out)

    @sync_action("testConnection")
    def test_connection(self) -> None:
        # The test-connection widget only needs {"status": "success"} (emitted by the platform when
        # this returns nothing) or a non-zero exit on failure — no message/ValidationResult.
        self.client.list_create_endpoints()  # forces token + a GraphQL round-trip; raises on failure

    @sync_action("list_endpoints")
    def list_endpoints(self) -> list[SelectElement]:
        return [SelectElement(value=e["value"], label=e["label"]) for e in self.client.list_create_endpoints()]

    @sync_action("list_agendas")
    def list_agendas(self) -> list[SelectElement]:
        return [SelectElement(value=a["value"], label=a["label"]) for a in self.client.list_agendas()]

    @staticmethod
    def _api_fields(fields: list[dict]) -> list[dict]:
        # Only scalar (non-list) fields are mappable; label == field_name so the dropdown shows the
        # real Money S3 API field name.
        return [{"field_name": f["field_name"], "label": f["field_name"]} for f in fields if not f["is_list"]]

    @staticmethod
    def _prefill(columns: list[str], field_names: list[str], existing: list) -> list[dict]:
        # Keep any mapping the user already set; fuzzy-match the rest against the API field names.
        kept = {fm.source_column: fm.destination_field for fm in existing}
        return [
            {"source_column": col, "destination_field": kept.get(col) or fuzzy_match(col, field_names)}
            for col in columns
        ]

    @sync_action("load_field_mapping")
    def load_field_mapping(self) -> dict:
        cfg = self.config
        if not cfg.endpoint:
            raise UserException("Please select an endpoint first.")
        tables = self.configuration.tables_input_mapping
        if not tables:
            raise UserException("Map at least one input table first, then re-load the mapping.")

        client = self.client
        try:
            _name, _arg, input_type = client.resolve_mutation(cfg.endpoint, cfg.write_mode)
        except UserException:
            # The mappable field universe is the same regardless of write mode, and most Money S3
            # endpoints expose only create<Endpoint>; fall back so "Re-load mapping" works in Update
            # mode too. The actual run still resolves the real write-mode mutation.
            _name, _arg, input_type = client.resolve_mutation(cfg.endpoint, "create")
        header_fields = client.get_input_fields(input_type.rstrip("!"))
        api_fields = self._api_fields(header_fields)

        # Document endpoints expose a line-item collection; surface its fields so the lines mapping
        # gets the same assisted dropdown (and prefill) as the header mapping.
        items_field, element_type = self._line_item_field(header_fields)
        line_api_fields = self._api_fields(client.get_input_fields(element_type)) if items_field else []

        # Split the mapped tables into header vs lines (the lines table is the one named in cfg).
        lines_tbl = next((t for t in tables if cfg.lines_table and t.destination == cfg.lines_table), None)
        header_tbl = next((t for t in tables if t is not lines_tbl), None)
        if header_tbl is None or not (header_tbl.columns or []):
            raise UserException("The header input table has no columns; select its columns in the input mapping.")

        data = {
            "field_mapping": self._prefill(
                header_tbl.columns, [f["field_name"] for f in api_fields], cfg.field_mapping
            ),
            "_metadata_": {"api_fields": api_fields, "line_api_fields": line_api_fields},
        }
        if lines_tbl is not None:
            # Echo the user's current line settings so the nested object isn't clobbered, plus the
            # prefilled line mapping.
            data["line_items"] = {
                "lines_table": cfg.lines_table,
                "lines_join_key": cfg.lines_join_key,
                "lines_field_mapping": self._prefill(
                    lines_tbl.columns or [], [f["field_name"] for f in line_api_fields], cfg.lines_field_mapping
                ),
            }
        return {"type": "data", "data": data}


if __name__ == "__main__":
    try:
        Component().execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
