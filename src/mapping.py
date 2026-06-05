import difflib


def _normalize(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def fuzzy_match(column: str, field_names: list[str]) -> str:
    norm_to_field = {_normalize(f): f for f in field_names}
    target = _normalize(column)
    if target in norm_to_field:
        return norm_to_field[target]
    close = difflib.get_close_matches(target, list(norm_to_field), n=1, cutoff=0.85)
    return norm_to_field[close[0]] if close else ""


def coerce_value(raw, gql_type: str):
    if raw is None or raw == "":
        return None
    if gql_type == "Boolean":
        return str(raw).strip().lower() in ("1", "true", "yes", "t")
    if gql_type == "Int":
        return int(raw)
    if gql_type == "Decimal":
        return float(raw)
    return raw  # String, Date, UUID, enums pass through as-is


def _assign(target: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = target
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def build_record_payload(row: dict, field_mapping: list, field_types: dict) -> dict:
    payload: dict = {}
    for fm in field_mapping:
        dest = fm.destination_field
        if not dest or fm.source_column not in row:
            continue
        value = coerce_value(row[fm.source_column], field_types.get(dest, "String"))
        if value is None:
            continue
        _assign(payload, dest, value)
    return payload


def build_document_payload(
    header_row, line_rows, header_mapping, line_mapping, header_types, line_types, items_field: str
) -> dict:
    payload = build_record_payload(header_row, header_mapping, header_types)
    payload[items_field] = [build_record_payload(lr, line_mapping, line_types) for lr in line_rows]
    return payload
