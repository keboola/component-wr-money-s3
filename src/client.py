import re
import time

import requests
from keboola.component.exceptions import UserException

TERMINAL_STATES = {"OK", "WARNING", "ERROR"}

_Q_MUTATIONS = (
    "{ __schema { mutationType { fields { name args { name "
    "type { kind name ofType { kind name ofType { kind name } } } } } } } }"
)
_Q_TYPE = '{ __type(name: "%s") { inputFields { name type { kind name ofType { kind name ofType { kind name } } } } } }'


def humanize(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name).strip()


def _unwrap(type_node: dict):
    """Return (base_name, base_kind, required, is_list) from a GraphQL type node."""
    required = type_node.get("kind") == "NON_NULL"
    is_list = False
    node = type_node
    while node and node.get("kind") in ("NON_NULL", "LIST"):
        if node["kind"] == "LIST":
            is_list = True
        node = node.get("ofType")
    return (node.get("name") if node else None), (node.get("kind") if node else None), required, is_list


def _render_type(type_node: dict) -> str:
    """Render a type node back into GraphQL SDL, e.g. 'CompanyInput!'."""
    kind = type_node.get("kind")
    if kind == "NON_NULL":
        return _render_type(type_node["ofType"]) + "!"
    if kind == "LIST":
        return "[" + _render_type(type_node["ofType"]) + "]"
    return type_node["name"]


class MoneyS3Client:
    def __init__(self, base_url, token_manager, session=None, agenda_id=None):
        self.graphql_url = base_url.rstrip("/") + "/graphql/"
        self.tokens = token_manager
        self.session = session or requests.Session()
        self.agenda_id = agenda_id

    def execute(self, query: str, variables: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.tokens.get_token()}", "Content-Type": "application/json"}
        if self.agenda_id:
            # Money S3 write operations are authorized against a specific agenda (company DB).
            headers["AgendaId"] = self.agenda_id
        try:
            resp = self.session.post(
                self.graphql_url, json={"query": query, "variables": variables or {}}, headers=headers, timeout=60
            )
        except requests.RequestException as e:
            raise UserException(f"Cannot reach Money S3 GraphQL endpoint {self.graphql_url}: {e}") from e
        if resp.status_code != 200:
            raise UserException(f"Money S3 GraphQL request failed ({resp.status_code}): {resp.text[:300]}")
        body = resp.json()
        if body.get("errors"):
            raise UserException(f"Money S3 GraphQL error: {body['errors']}")
        return body.get("data") or {}

    def _mutation_fields(self) -> list[dict]:
        return self.execute(_Q_MUTATIONS)["__schema"]["mutationType"]["fields"]

    def list_create_endpoints(self) -> list[dict]:
        out = []
        for f in self._mutation_fields():
            if f["name"].startswith("create"):
                obj = f["name"][len("create") :]
                out.append({"value": obj, "label": humanize(obj)})
        return sorted(out, key=lambda e: e["label"])

    def list_agendas(self) -> list[dict]:
        """List Money S3 agendas (company databases) as {value: guid, label: displayName}."""
        data = self.execute("{ agendas { items { guid displayName } } }")
        items = ((data.get("agendas") or {}).get("items")) or []
        return [{"value": a["guid"], "label": a.get("displayName") or a["guid"]} for a in items]

    def resolve_mutation(self, endpoint: str, write_mode: str):
        mutation_name = ("update" if write_mode == "update" else "create") + endpoint
        field = next((f for f in self._mutation_fields() if f["name"] == mutation_name), None)
        if not field:
            raise UserException(f"Money S3 has no '{mutation_name}' mutation for endpoint '{endpoint}'.")
        for arg in field["args"]:
            base, _kind, _req, _list = _unwrap(arg["type"])
            if base and base.endswith("Input") and base != "DefinitionXMLTransferRefInput":
                return mutation_name, arg["name"], _render_type(arg["type"])
        raise UserException(f"Could not determine the input argument of '{mutation_name}'.")

    def get_input_fields(self, input_type_name: str) -> list[dict]:
        t = self.execute(_Q_TYPE % input_type_name).get("__type") or {}
        fields = []
        for f in t.get("inputFields") or []:
            base, kind, required, is_list = _unwrap(f["type"])
            fields.append(
                {
                    "field_name": f["name"],
                    "label": humanize(f["name"]),
                    "type": base,
                    "kind": kind,
                    "required": required,
                    "is_list": is_list,
                }
            )
        return fields

    def submit(self, mutation_name: str, arg_name: str, input_type_render: str, payload: dict) -> str:
        query = f"mutation($input: {input_type_render}) {{ {mutation_name}({arg_name}: $input) {{ guid isSuccess }} }}"
        data = self.execute(query, {"input": payload})
        result = data.get(mutation_name) or {}
        guid = result.get("guid")
        if not guid:
            raise UserException(f"Money S3 did not return an import GUID for {mutation_name}: {result}")
        return guid

    def import_status(self, guid: str):
        query = "query($g: UUID!) { importStatus(importGuid: $g) { state stateInfo } }"
        data = self.execute(query, {"g": guid})
        status = data.get("importStatus") or {}
        return status.get("state"), status.get("stateInfo")

    def wait_for_import(self, guid: str, timeout_s: int = 60, interval_s: float = 1.0):
        """Poll importStatus until a terminal state. Returns the last observed (state, info);
        on timeout this may still be non-terminal (UNPROCESSED/IN_PROCESS) — the caller decides.
        A max-poll bound guarantees termination even if the wall clock does not advance (e.g. frozen
        time under test recording)."""
        deadline = time.time() + timeout_s
        max_polls = int(timeout_s / interval_s) if interval_s > 0 else None
        state, info = self.import_status(guid)
        polls = 1
        while state not in TERMINAL_STATES and time.time() < deadline and (max_polls is None or polls < max_polls):
            time.sleep(interval_s)
            state, info = self.import_status(guid)
            polls += 1
        return state, info
