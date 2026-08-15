#!/usr/bin/env python3
"""Execute compiler-bound, capability-gated Dataverse Web API operations."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

import pipeline_common as P


class ExecutorError(RuntimeError):
    """A safe, sanitized executor failure."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "validation_error",
        status: int | None = None,
        correlation_id: str = "",
        write_occurred: bool = False,
        action_invoked: bool = False,
        evidence_posted: bool = False,
    ) -> None:
        super().__init__(sanitize_text(message))
        self.category = category
        self.status = status
        self.correlation_id = correlation_id
        self.write_occurred = write_occurred
        self.action_invoked = action_invoked
        self.evidence_posted = evidence_posted


@dataclass(frozen=True)
class OperationRequest:
    method: str
    path: str
    body: dict[str, Any] | None
    parameter_names: tuple[str, ...]
    changed_fields: tuple[str, ...]
    solution_context: str
    description: str
    merge_labels: bool = False


@dataclass(frozen=True)
class HttpResult:
    status: int
    entity_id: str
    correlation_id: str
    data: dict[str, Any]


TOKEN_PATTERN = re.compile(
    r"(?i)(?:Bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"
)
GUID_RE = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
TYPE_VALUES = P.ENV_VARIABLE_TYPES
CONNECTOR_IDS = {
    "microsoft dataverse": "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps",
    "microsoft dataverse (legacy)": "/providers/Microsoft.PowerApps/apis/shared_commondataservice",
}
# Named entity-row components: authored as Dataverse rows keyed on a display
# name (plus a parent table for views and forms) rather than a compiler-owned
# schema name, so they resolve through row identity, not canonical_identity.
ROW_ENTITY_SETS = {
    "uiux_view": "savedqueries",
    "uiux_form": "systemforms",
    "sec_role": "roles",
}
ROW_ID_FIELDS = {
    "uiux_view": "savedqueryid",
    "uiux_form": "formid",
    "sec_role": "roleid",
}
# Documented solutioncomponent.componenttype values.
ROW_SOLUTION_COMPONENT_TYPES = {"uiux_view": 26, "uiux_form": 60, "sec_role": 20}
ROW_COMPONENT_TYPES = frozenset(ROW_ENTITY_SETS)
# SavedQuery.querytype values for the supported view surfaces.
VIEW_QUERYTYPES = {
    "public": 0,
    "advanced_find": 1,
    "associated": 2,
    "quick_find": 4,
    "lookup": 64,
}
# Conservative FetchXML condition operators the executor will emit.
VIEW_FILTER_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "gt",
        "ge",
        "lt",
        "le",
        "like",
        "not-like",
        "begins-with",
        "in",
        "not-null",
        "null",
        "on",
        "on-or-after",
        "on-or-before",
    }
)
# PrivilegeDepth enum members accepted by the AddPrivilegesRole bound action.
PRIVILEGE_DEPTHS = {
    "basic": "Basic",
    "local": "Local",
    "deep": "Deep",
    "global": "Global",
}
# Canonical privilege names (for example prvReadAccount) are compiler-owned, so
# the executor resolves their IDs by exact match rather than guessing casing.
PRIVILEGE_NAME_RE = re.compile(r"prv[A-Za-z][A-Za-z0-9_]*")
REQUIRED_LEVELS = P.REQUIRED_LEVELS
WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
RETRYABLE_READ_STATUS = {429, 502, 503, 504}
MSAL_VERSION = "1.37.0"
CASCADE_VALUES = P.CASCADE_VALUES
SAFE_PATH_METHODS = (
    (re.compile(r"^EntityDefinitions$"), {"POST"}),
    (
        re.compile(
            r"^EntityDefinitions\(LogicalName='[A-Za-z][A-Za-z0-9_]*'\)$"
        ),
        {"GET", "PUT", "DELETE"},
    ),
    (
        re.compile(
            r"^EntityDefinitions\(LogicalName='[A-Za-z][A-Za-z0-9_]*'\)/Attributes$"
        ),
        {"GET", "POST"},
    ),
    (
        re.compile(
            r"^EntityDefinitions\(LogicalName='[A-Za-z][A-Za-z0-9_]*'\)/"
            r"Attributes\(LogicalName='[A-Za-z][A-Za-z0-9_]*'\)$"
        ),
        {"GET", "PUT", "DELETE"},
    ),
    (
        re.compile(
            r"^EntityDefinitions\(LogicalName='[A-Za-z][A-Za-z0-9_]*'\)/Keys"
            r"(?:\(LogicalName='[A-Za-z][A-Za-z0-9_]*'\))?(?:\?.*)?$"
        ),
        {"GET", "POST", "PUT", "DELETE"},
    ),
    (re.compile(r"^RelationshipDefinitions$"), {"POST"}),
    (
        re.compile(
            r"^RelationshipDefinitions\((?:SchemaName='[A-Za-z][A-Za-z0-9_]*'|"
            r"MetadataId='[0-9a-fA-F-]{36}')\)$"
        ),
        {"GET", "PUT", "DELETE"},
    ),
    (
        re.compile(r"^RelationshipDefinitions\([0-9a-fA-F-]{36}\)$"),
        {"GET", "PUT", "DELETE"},
    ),
    (
        re.compile(r"^RelationshipDefinitions\?.*$"),
        {"GET"},
    ),
    (re.compile(r"^GlobalOptionSetDefinitions$"), {"POST"}),
    (
        re.compile(
            r"^GlobalOptionSetDefinitions\(Name='[A-Za-z][A-Za-z0-9_]*'\)$"
        ),
        {"GET", "DELETE"},
    ),
    (
        re.compile(r"^(?:UpdateOptionValue|PublishXml|AddSolutionComponent|RemoveSolutionComponent)$"),
        {"POST"},
    ),
    (
        re.compile(
            r"^(?:environmentvariabledefinitions|connectionreferences)"
            r"(?:\([0-9a-fA-F-]{36}\))?(?:\?.*)?$"
        ),
        {"GET", "POST", "PATCH", "DELETE"},
    ),
    (
        re.compile(r"^savedqueries(?:\([0-9a-fA-F-]{36}\))?(?:\?.*)?$"),
        {"GET", "POST", "PATCH", "DELETE"},
    ),
    (
        re.compile(r"^roles(?:\([0-9a-fA-F-]{36}\))?(?:\?.*)?$"),
        {"GET", "POST", "PATCH", "DELETE"},
    ),
    (
        re.compile(
            r"^roles\([0-9a-fA-F-]{36}\)/"
            r"Microsoft\.Dynamics\.CRM\.AddPrivilegesRole$"
        ),
        {"POST"},
    ),
    (re.compile(r"^privileges\?.*$"), {"GET"}),
    (re.compile(r"^businessunits\?.*$"), {"GET"}),
    (
        re.compile(
            r"^EntityDefinitions\(LogicalName='[A-Za-z][A-Za-z0-9_]*'\)\?.*$"
        ),
        {"GET"},
    ),
    (re.compile(r"^solutions\?.*$"), {"GET"}),
    (re.compile(r"^solutioncomponents\?.*$"), {"GET"}),
)


def sanitize_text(value: Any, maximum: int = 400) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = TOKEN_PATTERN.sub("[withheld:sensitive-pattern]", text)
    text = re.sub(
        r"(?i)\b(?:Authorization|Cookie|Set-Cookie|client_secret)\s*[:=]\s*\S+",
        "[withheld:sensitive-field]",
        text,
    )
    return text[:maximum]


def validate_runtime_request(method: str, path: str) -> None:
    if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
        raise ExecutorError("HTTP method is outside the executor whitelist")
    if (
        not path
        or path.startswith(("/", "\\"))
        or "://" in path
        or ".." in path
        or "#" in path
        or re.search(r"(?i)%2f|%5c", path)
    ):
        raise ExecutorError("request path is outside the executor whitelist")
    if not any(pattern.fullmatch(path) and method in methods for pattern, methods in SAFE_PATH_METHODS):
        raise ExecutorError(
            f"{method} request path is outside the executor whitelist"
        )


def validate_capability_request(
    capability: dict[str, Any], request: OperationRequest
) -> None:
    declared = capability["http"]
    if request.method != declared["method"]:
        raise ExecutorError(
            "constructed HTTP method does not match the compiler-owned capability"
        )
    template = declared["path_template"]
    escaped = re.escape(template)
    for placeholder, pattern in {
        "{identity}": r"[A-Za-z][A-Za-z0-9_]*",
        "{table}": r"[A-Za-z][A-Za-z0-9_]*",
        "{metadata_id}": r"[0-9a-fA-F-]{36}",
        "{record_id}": r"[0-9a-fA-F-]{36}",
    }.items():
        escaped = escaped.replace(re.escape(placeholder), pattern)
    if not re.fullmatch(escaped + r"(?:\?.*)?", request.path):
        raise ExecutorError(
            "constructed request path does not match the compiler-owned capability"
        )


def odata_string(value: str) -> str:
    return value.replace("'", "''")


def label(value: str) -> dict[str, Any]:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [
            {
                "@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                "Label": value,
                "LanguageCode": 1033,
            }
        ],
    }


def canonical_table(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\(([^()]+)\)\s*$", text)
    candidate = match.group(1) if match else text
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", candidate):
        raise ExecutorError("table identity is not a canonical logical name")
    return candidate.lower()


def canonical_identity(row: dict[str, Any]) -> tuple[str, str]:
    scope = row["implementation_scope"]
    field = (
        "schema_name"
        if scope == "repository_and_dataverse_solution"
        else "record_name"
    )
    value = str((row.get("payload") or {}).get(field) or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,99}", value):
        raise ExecutorError(f"compiler-owned {field} is missing or invalid")
    return field, value


def option_values(
    payload: dict[str, Any], auto_prefix: int | None = None
) -> list[tuple[int, str]]:
    parsed: list[tuple[int | None, str]] = []
    for raw in payload.get("options") or []:
        text = str(raw)
        match = re.fullmatch(r"\s*(\d{1,10})\s*:\s*(.{1,200})\s*", text)
        if match:
            parsed.append((int(match.group(1)), match.group(2)))
            continue
        bare = text.strip()
        if not bare or len(bare) > 200:
            raise ExecutorError(
                "choice options must use compiler-owned '<integer>: <label>' "
                "entries or a non-empty label"
            )
        parsed.append((None, bare))
    if not parsed:
        raise ExecutorError("choice payload has no options")
    explicit = [value for value, _ in parsed if value is not None]
    if len(set(explicit)) != len(explicit):
        raise ExecutorError("choice payload contains duplicate integer values")
    if all(value is not None for value, _ in parsed):
        return [(int(value), text) for value, text in parsed]
    if auto_prefix is None:
        raise ExecutorError(
            "choice options omit integer values but the authoring target declares "
            "no publisher_option_value_prefix to derive them from"
        )
    # Dataverse derives option values as <prefix><4-digit sequence>.
    used = set(explicit)
    base = auto_prefix * 10000
    offset = 0
    resolved: list[tuple[int, str]] = []
    for value, text in parsed:
        if value is None:
            while base + offset in used:
                offset += 1
            value = base + offset
            used.add(value)
            offset += 1
        resolved.append((int(value), text))
    return resolved


def required_level(payload: dict[str, Any]) -> str:
    raw = P.normalize_required_level(payload.get("required_level"))
    if raw not in REQUIRED_LEVELS:
        raise ExecutorError(f"unsupported required level '{raw}'")
    return REQUIRED_LEVELS[raw]


def column_definition(column: dict[str, Any]) -> dict[str, Any]:
    schema_name = str(column.get("schema_name") or column.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,99}", schema_name):
        raise ExecutorError("column payload has no canonical schema name")
    display = str(column.get("display_name") or "").strip() or P.display_label(
        column.get("name"), schema_name
    )
    data_type = str(column.get("data_type") or "").strip()
    common = {
        "SchemaName": schema_name,
        "DisplayName": label(display),
        "RequiredLevel": {
            "Value": required_level(column),
            "CanBeChanged": True,
            "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings",
        },
    }
    matched = P.match_column_data_type(data_type)
    if matched is None:
        raise ExecutorError(f"unsupported compiler column data_type '{data_type}'")
    kind, groups = matched
    if kind == "choice":
        common.update(
            {
                "@odata.type": "Microsoft.Dynamics.CRM.PicklistAttributeMetadata",
                "AttributeType": "Picklist",
                "AttributeTypeName": {"Value": "PicklistType"},
                "GlobalOptionSet@odata.bind": (
                    "/GlobalOptionSetDefinitions(Name='"
                    + odata_string(groups.group(1))
                    + "')"
                ),
            }
        )
    elif kind == "multiline":
        common.update(
            {
                "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
                "AttributeType": "Memo",
                "AttributeTypeName": {"Value": "MemoType"},
                "Format": "TextArea",
                "MaxLength": int(groups.group(1) or 2000),
            }
        )
    elif kind == "text":
        common.update(
            {
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
                "AttributeType": "String",
                "AttributeTypeName": {"Value": "StringType"},
                "FormatName": {"Value": "Text"},
                "MaxLength": int(groups.group(1) or 100),
            }
        )
    elif kind == "integer":
        common.update(
            {
                "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
                "AttributeType": "Integer",
                "AttributeTypeName": {"Value": "IntegerType"},
                "Format": "None",
                "MinValue": int(groups.group(1) or -2147483648),
                "MaxValue": 2147483647,
            }
        )
    else:  # boolean
        common.update(
            {
                "@odata.type": "Microsoft.Dynamics.CRM.BooleanAttributeMetadata",
                "AttributeType": "Boolean",
                "AttributeTypeName": {"Value": "BooleanType"},
                "DefaultValue": bool(column.get("default_value", False)),
                "OptionSet": {
                    "TrueOption": {"Value": 1, "Label": label("Yes")},
                    "FalseOption": {"Value": 0, "Label": label("No")},
                },
            }
        )
    return common


def table_create_body(payload: dict[str, Any], identity: str) -> dict[str, Any]:
    if str(payload.get("operation") or "").lower() not in {"create", "new"}:
        raise ExecutorError(
            "schema_table create is allowed only when payload.operation is create"
        )
    primary = str(payload.get("primary_name") or "").strip()
    if not primary:
        raise ExecutorError("schema_table payload has no primary_name")
    primary_schema = (
        primary
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,99}", primary)
        else identity + "Name"
    )
    ownership = str(payload.get("ownership") or "").lower()
    ownership_type = "OrganizationOwned" if "organization" in ownership else "UserOwned"
    attributes = [
        {
            "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            "AttributeType": "String",
            "AttributeTypeName": {"Value": "StringType"},
            "SchemaName": primary_schema,
            "DisplayName": label(primary),
            "IsPrimaryName": True,
            "RequiredLevel": {
                "Value": "ApplicationRequired",
                "CanBeChanged": True,
                "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings",
            },
            "FormatName": {"Value": "Text"},
            "MaxLength": 200,
        }
    ]
    for column in payload.get("columns") or []:
        if isinstance(column, dict):
            attributes.append(column_definition(column))
    display = str(payload.get("display_name") or "").strip() or P.display_label(
        payload.get("name"), identity, drop_suffixes=("skeleton",)
    )
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
        "SchemaName": identity,
        "DisplayName": label(display),
        "DisplayCollectionName": label(display),
        "OwnershipType": ownership_type,
        "IsActivity": False,
        "HasActivities": False,
        "HasNotes": False,
        "Attributes": attributes,
    }


def relationship_body(payload: dict[str, Any], identity: str) -> dict[str, Any]:
    relation_type = str(payload.get("relationship_type") or "").lower()
    left = canonical_table(payload.get("table"))
    right = canonical_table(payload.get("related_table"))
    if relation_type in {"many_to_many", "many-to-many", "n:n"}:
        return {
            "@odata.type": "Microsoft.Dynamics.CRM.ManyToManyRelationshipMetadata",
            "SchemaName": identity,
            "Entity1LogicalName": left,
            "Entity2LogicalName": right,
            "IntersectEntityName": identity.lower(),
        }
    if relation_type not in {"one_to_many", "one-to-many", "1:n", "many_to_one", "n:1"}:
        raise ExecutorError(
            "relationship_type must be one_to_many, many_to_one, or many_to_many"
        )
    lookup = str(payload.get("lookup_column") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,99}", lookup):
        raise ExecutorError(
            "one-to-many relationship payload requires canonical lookup_column"
        )
    referenced, referencing = (
        (right, left) if relation_type in {"many_to_one", "n:1"} else (left, right)
    )
    referenced_attribute = str(payload.get("referenced_attribute") or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,99}", referenced_attribute):
        raise ExecutorError(
            "one-to-many relationship payload requires referenced_attribute"
        )
    raw_cascade = payload.get("cascade_configuration")
    if not isinstance(raw_cascade, dict):
        raise ExecutorError(
            "one-to-many relationship payload requires cascade_configuration"
        )
    cascade = {}
    for key in P.CASCADE_ACTIONS:
        value = str(raw_cascade.get(key) or "")
        if value not in CASCADE_VALUES:
            raise ExecutorError(
                f"cascade_configuration.{key} must use a documented cascade value"
            )
        cascade[key] = value
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata",
        "SchemaName": identity,
        "ReferencedEntity": referenced,
        "ReferencedAttribute": referenced_attribute,
        "ReferencingEntity": referencing,
        "CascadeConfiguration": cascade,
        "Lookup": {
            "@odata.type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata",
            "AttributeType": "Lookup",
            "AttributeTypeName": {"Value": "LookupType"},
            "SchemaName": lookup,
            "DisplayName": label(str(payload.get("name") or lookup)),
            "RequiredLevel": {
                "Value": required_level(payload),
                "CanBeChanged": True,
                "ManagedPropertyLogicalName": "canmodifyrequirementlevelsettings",
            },
        },
    }


def row_component_name(row: dict[str, Any]) -> str:
    name = str((row.get("payload") or {}).get("name") or "").strip()
    if not name or len(name) > 200 or re.search(r"[\x00-\x1f]", name):
        raise ExecutorError("row component display name is missing or invalid")
    return name


def canonical_column(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", text):
        raise ExecutorError("column is not a canonical logical name")
    return text


def xml_attr(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def view_querytype(payload: dict[str, Any]) -> int:
    querytype = VIEW_QUERYTYPES.get(
        str(payload.get("view_type") or "public").strip().lower()
    )
    if querytype is None:
        raise ExecutorError("view_type is not a supported saved-query surface")
    return querytype


def view_columns(payload: dict[str, Any]) -> list[tuple[str, int]]:
    raw = payload.get("columns")
    if not isinstance(raw, list) or not raw:
        raise ExecutorError("view payload requires at least one column")
    widths = payload.get("widths") or {}
    if not isinstance(widths, dict):
        raise ExecutorError("view widths must map column names to pixel widths")
    resolved: list[tuple[str, int]] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, dict):
            name = canonical_column(entry.get("name"))
            width = entry.get("width", widths.get(name, 100))
        else:
            name = canonical_column(entry)
            width = widths.get(name, 100)
        try:
            width = int(width)
        except (TypeError, ValueError):
            raise ExecutorError("view column width must be an integer")
        if not 1 <= width <= 2000:
            raise ExecutorError("view column width is out of range")
        if name in seen:
            raise ExecutorError("view columns must be unique")
        seen.add(name)
        resolved.append((name, width))
    return resolved


def view_conditions(payload: dict[str, Any]) -> list[str]:
    conditions: list[str] = []
    for entry in payload.get("filters") or []:
        if not isinstance(entry, dict):
            raise ExecutorError("view filter entries must be mappings")
        column = canonical_column(entry.get("column"))
        operator = str(entry.get("operator") or "").strip().lower()
        if operator not in VIEW_FILTER_OPERATORS:
            raise ExecutorError(
                "view filter operator is not on the executor whitelist"
            )
        if operator in {"null", "not-null"}:
            conditions.append(
                f'<condition attribute="{xml_attr(column)}" '
                f'operator="{operator}" />'
            )
        elif operator == "in":
            values = entry.get("value")
            if not isinstance(values, list) or not values:
                raise ExecutorError(
                    "view 'in' filter requires a non-empty value list"
                )
            inner = "".join(f"<value>{xml_attr(item)}</value>" for item in values)
            conditions.append(
                f'<condition attribute="{xml_attr(column)}" operator="in">'
                f"{inner}</condition>"
            )
        else:
            if "value" not in entry:
                raise ExecutorError("view filter requires a value")
            conditions.append(
                f'<condition attribute="{xml_attr(column)}" '
                f'operator="{operator}" value="{xml_attr(entry["value"])}" />'
            )
    return conditions


def view_orders(payload: dict[str, Any]) -> list[str]:
    orders: list[str] = []
    for entry in payload.get("sorts") or []:
        if not isinstance(entry, dict):
            raise ExecutorError("view sort entries must be mappings")
        column = canonical_column(entry.get("column"))
        descending = "true" if entry.get("descending") else "false"
        orders.append(
            f'<order attribute="{xml_attr(column)}" descending="{descending}" />'
        )
    return orders


def view_fetchxml(
    payload: dict[str, Any], table: str, columns: list[tuple[str, int]]
) -> str:
    attributes = "".join(f'<attribute name="{xml_attr(n)}" />' for n, _ in columns)
    orders = "".join(view_orders(payload))
    conditions = view_conditions(payload)
    filter_xml = (
        f'<filter type="and">{"".join(conditions)}</filter>' if conditions else ""
    )
    return (
        '<fetch version="1.0" mapping="logical" no-lock="false">'
        f'<entity name="{xml_attr(table)}">{attributes}{orders}{filter_xml}'
        "</entity></fetch>"
    )


def view_layoutxml(
    columns: list[tuple[str, int]],
    object_type_code: str,
    primary_id_attribute: str,
) -> str:
    cells = "".join(f'<cell name="{xml_attr(n)}" width="{w}" />' for n, w in columns)
    return (
        f'<grid name="resultset" object="{xml_attr(object_type_code)}" '
        f'jump="{xml_attr(columns[0][0])}" select="1" icon="1" preview="1">'
        f'<row name="result" id="{xml_attr(primary_id_attribute)}">{cells}</row>'
        "</grid>"
    )


def build_row_requests(
    row: dict[str, Any],
    operation: str,
    context: str,
    *,
    resolved_id: str,
    object_type_code: str,
    primary_id_attribute: str,
    business_unit_id: str,
) -> list[OperationRequest]:
    payload = row["payload"]
    component_type = row["component_type"]
    entity_set = ROW_ENTITY_SETS[component_type]
    name = row_component_name(row)
    if component_type == "uiux_view":
        table = canonical_table(payload.get("table"))
        querytype = view_querytype(payload)
        if operation in {"create", "update"}:
            columns = view_columns(payload)
            body: dict[str, Any] = {
                "name": name,
                "returnedtypecode": table,
                "fetchxml": view_fetchxml(payload, table, columns),
                "layoutxml": view_layoutxml(
                    columns, object_type_code, primary_id_attribute
                ),
                "querytype": querytype,
            }
            if payload.get("description"):
                body["description"] = str(payload["description"])
            if operation == "create":
                return [
                    OperationRequest(
                        "POST",
                        entity_set,
                        body,
                        tuple(body),
                        ("name", "returnedtypecode", "fetchxml"),
                        context,
                        "create exact saved-query view",
                    )
                ]
            patch = {
                key: value
                for key, value in body.items()
                if key not in {"returnedtypecode", "querytype"}
            }
            return [
                OperationRequest(
                    "PATCH",
                    f"{entity_set}({resolved_id})",
                    patch,
                    tuple(patch),
                    ("name", "fetchxml", "layoutxml"),
                    context,
                    "update exact saved-query view",
                )
            ]
        if operation == "delete":
            return [
                OperationRequest(
                    "DELETE",
                    f"{entity_set}({resolved_id})",
                    None,
                    (),
                    (),
                    context,
                    "delete exact saved-query view",
                )
            ]
        if operation == "verify":
            query = urlencode(
                {
                    "$select": "savedqueryid,name,returnedtypecode,querytype",
                    "$filter": (
                        f"name eq '{odata_string(name)}' and "
                        f"returnedtypecode eq '{odata_string(table)}' and "
                        f"querytype eq {querytype}"
                    ),
                }
            )
            return [
                OperationRequest(
                    "GET",
                    f"{entity_set}?{query}",
                    None,
                    (),
                    (),
                    "none",
                    "verify exact saved-query view",
                )
            ]
    if component_type == "sec_role":
        if operation in {"create", "update"}:
            body: dict[str, Any] = {"name": name}
            if operation == "create":
                body["businessunitid@odata.bind"] = (
                    f"/businessunits({business_unit_id})"
                )
                return [
                    OperationRequest(
                        "POST",
                        entity_set,
                        body,
                        tuple(body),
                        ("name",),
                        context,
                        "create exact security role",
                    )
                ]
            return [
                OperationRequest(
                    "PATCH",
                    f"{entity_set}({resolved_id})",
                    body,
                    tuple(body),
                    ("name",),
                    context,
                    "update exact security role",
                )
            ]
        if operation == "delete":
            return [
                OperationRequest(
                    "DELETE",
                    f"{entity_set}({resolved_id})",
                    None,
                    (),
                    (),
                    context,
                    "delete exact security role",
                )
            ]
        if operation == "verify":
            query = urlencode(
                {
                    "$select": "roleid,name",
                    "$filter": f"name eq '{odata_string(name)}'",
                }
            )
            return [
                OperationRequest(
                    "GET",
                    f"{entity_set}?{query}",
                    None,
                    (),
                    (),
                    "none",
                    "verify exact security role",
                )
            ]
    raise ExecutorError(
        f"executor has no whitelisted row builder for {component_type}.{operation}"
    )


def build_static_requests(
    row: dict[str, Any],
    operation: str,
    capability: dict[str, Any],
    *,
    resolved_id: str = "{record_id}",
    metadata_id: str = "{metadata_id}",
    object_type_code: str = "{object_type_code}",
    primary_id_attribute: str = "{primary_id_attribute}",
    business_unit_id: str = "{business_unit_id}",
) -> list[OperationRequest]:
    payload = row["payload"]
    component_type = row["component_type"]
    mechanism = capability["solution_context"]["mechanism"]
    context = (
        "header"
        if mechanism == "MSCRM.SolutionUniqueName"
        else "action_parameter"
        if mechanism == "action_parameter"
        else "none"
    )
    if component_type in ROW_COMPONENT_TYPES:
        return build_row_requests(
            row,
            operation,
            context,
            resolved_id=resolved_id,
            object_type_code=object_type_code,
            primary_id_attribute=primary_id_attribute,
            business_unit_id=business_unit_id,
        )
    _, identity = canonical_identity(row)
    if component_type == "schema_table":
        if operation == "create":
            return [
                OperationRequest(
                    "POST",
                    "EntityDefinitions",
                    table_create_body(payload, identity),
                    ("SchemaName", "OwnershipType", "Attributes"),
                    ("SchemaName", "Attributes"),
                    context,
                    "create exact table definition",
                )
            ]
        if operation == "update" and str(payload.get("operation") or "").lower() == "extend":
            table = canonical_table(payload.get("table") or identity)
            return [
                OperationRequest(
                    "POST",
                    f"EntityDefinitions(LogicalName='{odata_string(table)}')/Attributes",
                    column_definition(column),
                    ("SchemaName", "AttributeType", "RequiredLevel"),
                    ("SchemaName", "AttributeType", "RequiredLevel"),
                    context,
                    "create exact child column for table extension",
                )
                for column in payload.get("columns") or []
                if isinstance(column, dict)
            ]
        path = f"EntityDefinitions(LogicalName='{odata_string(identity)}')"
        if operation == "delete":
            return [
                OperationRequest(
                    "DELETE", path, None, (), (), context, "delete exact table definition"
                )
            ]
        if operation == "verify":
            return [
                OperationRequest("GET", path, None, (), (), "none", "verify exact table")
            ]
    if component_type == "schema_column":
        table = canonical_table(payload.get("table"))
        path = (
            f"EntityDefinitions(LogicalName='{odata_string(table)}')/"
            f"Attributes(LogicalName='{odata_string(identity)}')"
        )
        if operation == "create":
            return [
                OperationRequest(
                    "POST",
                    f"EntityDefinitions(LogicalName='{odata_string(table)}')/Attributes",
                    column_definition(payload),
                    ("SchemaName", "AttributeType", "RequiredLevel"),
                    ("SchemaName", "AttributeType", "RequiredLevel"),
                    context,
                    "create exact column definition",
                )
            ]
        if operation == "update":
            body = column_definition(payload)
            return [
                OperationRequest(
                    "PUT",
                    path,
                    body,
                    tuple(body),
                    ("DisplayName", "RequiredLevel"),
                    context,
                    "retrieve, merge, and replace exact column definition",
                    merge_labels=True,
                )
            ]
        if operation == "delete":
            return [
                OperationRequest("DELETE", path, None, (), (), context, "delete exact column")
            ]
        if operation == "verify":
            return [
                OperationRequest("GET", path, None, (), (), "none", "verify exact column")
            ]
    if component_type == "schema_relationship":
        if operation == "create":
            return [
                OperationRequest(
                    "POST",
                    "RelationshipDefinitions",
                    relationship_body(payload, identity),
                    ("SchemaName", "@odata.type"),
                    ("SchemaName",),
                    context,
                    "create exact relationship definition",
                )
            ]
        if operation in {"delete", "update"}:
            method = "DELETE" if operation == "delete" else "PUT"
            body = None if operation == "delete" else relationship_body(payload, identity)
            return [
                OperationRequest(
                    method,
                    f"RelationshipDefinitions({metadata_id})",
                    body,
                    tuple(body.keys()) if body else (),
                    ("SchemaName",) if body else (),
                    context,
                    f"{operation} exact relationship definition",
                    merge_labels=operation == "update",
                )
            ]
        if operation == "verify":
            return [
                OperationRequest(
                    "GET",
                    f"RelationshipDefinitions(SchemaName='{odata_string(identity)}')",
                    None,
                    (),
                    (),
                    "none",
                    "verify exact relationship",
                )
            ]
    if component_type == "schema_choice":
        path = f"GlobalOptionSetDefinitions(Name='{odata_string(identity)}')"
        if operation == "create":
            options = [
                {"Value": value, "Label": label(option_label)}
                for value, option_label in option_values(
                    payload,
                    (row.get("authoring_target") or {}).get(
                        "publisher_option_value_prefix"
                    ),
                )
            ]
            body = {
                "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
                "Name": identity,
                "DisplayName": label(
                    str(payload.get("display_name") or "").strip()
                    or P.display_label(
                        payload.get("name"),
                        identity,
                        drop_suffixes=("global choice",),
                    )
                ),
                "Options": options,
            }
            return [
                OperationRequest(
                    "POST",
                    "GlobalOptionSetDefinitions",
                    body,
                    ("Name", "DisplayName", "OptionSetType", "Options"),
                    ("Name", "Options"),
                    context,
                    "create exact global choice",
                )
            ]
        if operation == "update":
            return [
                OperationRequest(
                    "POST",
                    "UpdateOptionValue",
                    {
                        "OptionSetName": identity,
                        "Value": value,
                        "Label": label(option_label),
                        "MergeLabels": True,
                        "ParentValues": [],
                        "SolutionUniqueName": row["authoring_target"][
                            "solution_unique_name"
                        ],
                    },
                    (
                        "OptionSetName",
                        "Value",
                        "Label",
                        "MergeLabels",
                        "ParentValues",
                        "SolutionUniqueName",
                    ),
                    ("Options",),
                    "action_parameter",
                    "update exact existing global choice option",
                )
                for value, option_label in option_values(payload)
            ]
        if operation == "delete":
            return [
                OperationRequest("DELETE", path, None, (), (), context, "delete exact choice")
            ]
        if operation == "verify":
            return [
                OperationRequest("GET", path, None, (), (), "none", "verify exact choice")
            ]
    if component_type == "schema_key":
        table = canonical_table(payload.get("table"))
        collection = f"EntityDefinitions(LogicalName='{odata_string(table)}')/Keys"
        if operation == "create":
            body = {
                "@odata.type": "Microsoft.Dynamics.CRM.EntityKeyMetadata",
                "SchemaName": identity,
                "KeyAttributes": [str(item) for item in payload.get("key_columns") or []],
                "DisplayName": label(str(payload.get("name") or identity)),
            }
            return [
                OperationRequest(
                    "POST",
                    collection,
                    body,
                    ("SchemaName", "KeyAttributes", "DisplayName"),
                    ("SchemaName", "KeyAttributes"),
                    context,
                    "create exact alternate key",
                )
            ]
        if operation == "delete":
            return [
                OperationRequest(
                    "DELETE",
                    f"{collection}(LogicalName='{odata_string(identity)}')",
                    None,
                    (),
                    (),
                    context,
                    "delete exact alternate key",
                )
            ]
        if operation == "verify":
            return [
                OperationRequest(
                    "GET", collection, None, (), (), "none", "verify exact alternate key"
                )
            ]
    if component_type == "config_env_variable":
        if operation in {"create", "update"}:
            data_type = str(payload.get("data_type") or "").strip().lower()
            if data_type not in TYPE_VALUES:
                raise ExecutorError(f"unsupported environment-variable type '{data_type}'")
            body = {
                "schemaname": identity,
                "displayname": str(payload.get("name") or identity),
                "type": TYPE_VALUES[data_type],
            }
            if "default_value" in payload and data_type != "secret":
                body["defaultvalue"] = str(payload["default_value"])
            method = "POST" if operation == "create" else "PATCH"
            path = (
                "environmentvariabledefinitions"
                if operation == "create"
                else f"environmentvariabledefinitions({resolved_id})"
            )
            return [
                OperationRequest(
                    method,
                    path,
                    body,
                    tuple(body),
                    ("schemaname", "displayname", "type"),
                    context,
                    f"{operation} exact environment-variable definition",
                )
            ]
        if operation == "delete":
            return [
                OperationRequest(
                    "DELETE",
                    f"environmentvariabledefinitions({resolved_id})",
                    None,
                    (),
                    (),
                    context,
                    "delete exact environment-variable definition",
                )
            ]
        if operation == "verify":
            query = urlencode(
                {
                    "$select": "environmentvariabledefinitionid,schemaname,displayname,type",
                    "$filter": f"schemaname eq '{odata_string(identity)}'",
                }
            )
            return [
                OperationRequest(
                    "GET",
                    "environmentvariabledefinitions?" + query,
                    None,
                    (),
                    (),
                    "none",
                    "verify exact environment-variable definition",
                )
            ]
    if component_type == "integ_connection_ref":
        connector = str(payload.get("connector") or "").strip().lower()
        if connector not in CONNECTOR_IDS:
            raise ExecutorError(
                "connector is not in the executor's official standard-connector whitelist"
            )
        if operation in {"create", "update"}:
            body = {
                "connectionreferencelogicalname": identity,
                "connectionreferencedisplayname": str(payload.get("name") or identity),
                "connectorid": CONNECTOR_IDS[connector],
            }
            method = "POST" if operation == "create" else "PATCH"
            path = (
                "connectionreferences"
                if operation == "create"
                else f"connectionreferences({resolved_id})"
            )
            return [
                OperationRequest(
                    method,
                    path,
                    body,
                    tuple(body),
                    tuple(body),
                    context,
                    f"{operation} exact connection reference",
                )
            ]
        if operation == "delete":
            return [
                OperationRequest(
                    "DELETE",
                    f"connectionreferences({resolved_id})",
                    None,
                    (),
                    (),
                    context,
                    "delete exact connection reference",
                )
            ]
        if operation == "verify":
            query = urlencode(
                {
                    "$select": "connectionreferenceid,connectionreferencelogicalname,connectionreferencedisplayname,connectorid",
                    "$filter": (
                        "connectionreferencelogicalname eq "
                        f"'{odata_string(identity)}'"
                    ),
                }
            )
            return [
                OperationRequest(
                    "GET",
                    "connectionreferences?" + query,
                    None,
                    (),
                    (),
                    "none",
                    "verify exact connection reference",
                )
            ]
    if operation == "publish":
        table = canonical_table(payload.get("table") or identity)
        if component_type == "schema_choice":
            parameter_xml = (
                "<importexportxml><optionsets><optionset>"
                + identity
                + "</optionset></optionsets></importexportxml>"
            )
        else:
            parameter_xml = (
                "<importexportxml><entities><entity>"
                + table
                + "</entity></entities></importexportxml>"
            )
        return [
            OperationRequest(
                "POST",
                "PublishXml",
                {"ParameterXml": parameter_xml},
                ("ParameterXml",),
                ("published_customizations",),
                "none",
                "publish only the exact component scope",
            )
        ]
    raise ExecutorError(
        f"executor has no whitelisted builder for {component_type}.{operation}"
    )


def merge_metadata_update(
    row: dict[str, Any],
    request: OperationRequest,
    client: DataverseClient,
) -> OperationRequest:
    if request.method != "PUT" or row["component_type"] not in {
        "schema_column",
        "schema_relationship",
    }:
        return request
    if row["component_type"] == "schema_relationship":
        _, identity = canonical_identity(row)
        read_path = (
            "RelationshipDefinitions"
            f"(SchemaName='{odata_string(identity)}')"
        )
    else:
        read_path = request.path
    current = client.request(
        OperationRequest(
            "GET",
            read_path,
            None,
            (),
            (),
            "none",
            "retrieve exact metadata definition required for PUT replacement",
        )
    ).data
    if not current:
        raise ExecutorError(
            "metadata update target could not be retrieved",
            category="not_found",
        )
    merged = dict(current)
    merged.pop("@odata.context", None)
    merged.pop("@odata.etag", None)
    merged.update(request.body or {})
    return OperationRequest(
        request.method,
        request.path,
        merged,
        request.parameter_names,
        request.changed_fields,
        request.solution_context,
        request.description,
        merge_labels=True,
    )


class DataverseClient:
    def __init__(self, service_root: str, token: str, solution_name: str) -> None:
        parsed = urlsplit(service_root)
        if (
            parsed.scheme != "https"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ExecutorError("compiler-owned Dataverse service root is invalid")
        self.service_root = service_root.rstrip("/")
        self.token = token
        self.solution_name = solution_name
        self.request_count = 0

    def request(self, operation: OperationRequest) -> HttpResult:
        validate_runtime_request(operation.method, operation.path)
        self.request_count += 1
        url = self.service_root + "/" + operation.path.lstrip("/")
        headers = {
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
            "If-None-Match": "null",
            "Authorization": "Bearer " + self.token,
        }
        data = None
        if operation.body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
            data = json.dumps(operation.body, ensure_ascii=False).encode("utf-8")
        if operation.solution_context == "header":
            headers["MSCRM.SolutionUniqueName"] = self.solution_name
        if operation.merge_labels:
            headers["MSCRM.MergeLabels"] = "true"
        attempts = 3 if operation.method == "GET" else 1
        for attempt in range(attempts):
            request = Request(url, data=data, headers=headers, method=operation.method)
            try:
                with urlopen(request, timeout=60) as response:
                    raw = response.read()
                    parsed_data = (
                        json.loads(raw.decode("utf-8"))
                        if raw and response.headers.get_content_type() == "application/json"
                        else {}
                    )
                    entity_id = response.headers.get("OData-EntityId", "")
                    correlation = (
                        response.headers.get("x-ms-service-request-id")
                        or response.headers.get("REQ_ID")
                        or ""
                    )
                    return HttpResult(
                        response.status,
                        entity_id,
                        sanitize_text(correlation, 100),
                        parsed_data if isinstance(parsed_data, dict) else {},
                    )
            except HTTPError as exc:
                if (
                    operation.method == "GET"
                    and exc.code in RETRYABLE_READ_STATUS
                    and attempt + 1 < attempts
                ):
                    delay = min(int(exc.headers.get("Retry-After", "1") or "1"), 5)
                    time.sleep(max(delay, 1))
                    continue
                raw = exc.read()
                code = ""
                try:
                    parsed = json.loads(raw.decode("utf-8")) if raw else {}
                    error = (
                        parsed.get("error")
                        if isinstance(parsed, dict)
                        and isinstance(parsed.get("error"), dict)
                        else {}
                    )
                    code = str(error.get("code") or "")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                safe_code = (
                    code
                    if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code)
                    else ""
                )
                correlation = (
                    exc.headers.get("x-ms-service-request-id")
                    or exc.headers.get("REQ_ID")
                    or ""
                )
                raise ExecutorError(
                    f"Dataverse returned HTTP {exc.code}"
                    + (f" ({safe_code})" if safe_code else ""),
                    category=error_category(exc.code, safe_code),
                    status=exc.code,
                    correlation_id=sanitize_text(correlation, 100),
                ) from None
            except (URLError, TimeoutError, OSError):
                raise ExecutorError(
                    "Dataverse request failed or timed out",
                    category="unavailable",
                ) from None
        raise ExecutorError("Dataverse read retry budget exhausted", category="unavailable")


def error_category(status: int | None, code: str = "") -> str:
    if status == 401:
        return "unauthenticated"
    if status == 403:
        return "permission_or_client_allowlist"
    if status == 404:
        return "not_found"
    if status in {409, 412}:
        return "conflict_or_duplicate"
    if status in {400, 422}:
        return "validation_error"
    if status in RETRYABLE_READ_STATUS:
        return "unavailable"
    return sanitize_text(code or "platform_error", 80)


def validate_oauth_public_client(row: dict[str, Any]) -> dict[str, str]:
    resource = next(
        (
            item
            for item in row["development_resources"].get("required") or []
            if item["id"] == "dataverse-web-api"
        ),
        None,
    )
    config = (resource or {}).get("oauth_public_client") or {}
    client_id = str(config.get("client_id") or "")
    tenant_id = str(config.get("tenant_id") or "")
    if P.UNRESOLVED_AUTHORING_VALUE in {client_id, tenant_id}:
        raise ExecutorError(
            "Dataverse Web API public-client client_id and tenant_id must be configured",
            category="configuration_prerequisite",
        )
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", client_id):
        raise ExecutorError("configured public-client client_id must be a GUID")
    if not re.fullmatch(r"[A-Za-z0-9.-]{2,253}", tenant_id):
        raise ExecutorError("configured tenant_id is invalid")
    scope_suffix = str(config.get("scope_suffix") or "")
    redirect_uri = str(config.get("redirect_uri") or "")
    if scope_suffix != "/user_impersonation":
        raise ExecutorError("configured public-client scope_suffix is invalid")
    if redirect_uri != "http://localhost":
        raise ExecutorError("configured public-client redirect_uri is invalid")
    try:
        installed = importlib.metadata.version("msal")
    except importlib.metadata.PackageNotFoundError:
        raise ExecutorError(
            f"MSAL {MSAL_VERSION} is required", category="configuration_prerequisite"
        ) from None
    if installed != MSAL_VERSION:
        raise ExecutorError(
            f"MSAL version mismatch: expected {MSAL_VERSION}, found {installed}",
            category="configuration_prerequisite",
        )
    return {
        "client_id": client_id,
        "tenant_id": tenant_id,
        "scope_suffix": scope_suffix,
        "redirect_uri": redirect_uri,
    }


def acquire_token(row: dict[str, Any], policy: str) -> str:
    config = validate_oauth_public_client(row)
    try:
        import msal
    except ImportError:
        raise ExecutorError("MSAL import failed", category="configuration_prerequisite")
    logging.getLogger("msal").disabled = True
    authority = f"https://login.microsoftonline.com/{config['tenant_id']}"
    cache = msal.TokenCache()
    app = msal.PublicClientApplication(
        client_id=config["client_id"], authority=authority, token_cache=cache
    )
    environment_url = row["authoring_target"]["environment_url"].rstrip("/")
    scopes = [environment_url + config["scope_suffix"]]
    result: dict[str, Any] | None = None
    if policy == "reuse_if_valid":
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result or "access_token" not in result:
        kwargs: dict[str, Any] = {
            "scopes": scopes,
            "redirect_uri": config["redirect_uri"],
        }
        if policy == "always_prompt":
            kwargs["prompt"] = "select_account"
        result = app.acquire_token_interactive(**kwargs)
    token = str((result or {}).get("access_token") or "")
    if not token:
        raise ExecutorError(
            "interactive authentication did not return an access token",
            category="unauthenticated",
        )
    return token


def load_row(dev_id: str) -> tuple[dict[str, Any], Path]:
    context = P.read_context(P.TASK_CONTEXT_PATH)
    rows = [row for row in context.get("tasks") or [] if row.get("id") == dev_id]
    if len(rows) != 1:
        raise ExecutorError(f"{dev_id} does not resolve to one current task")
    row = dict(rows[0])
    row["task_context_hash"] = context["context_hash"]
    path = P.ROOT / row["workspace"] / "development" / f"{dev_id}.md"
    front, _, _ = P.read_markdown(path)
    if front.get("task_context_hash") != context.get("context_hash"):
        raise ExecutorError("DEV task_context_hash is stale")
    if front.get("source_plan_hash") != row.get("source_plan_hash"):
        raise ExecutorError("DEV source_plan_hash is stale")
    if front.get("component") != row.get("component"):
        raise ExecutorError("DEV canonical component binding is stale")
    if front.get("authoring_target") != row.get("authoring_target"):
        raise ExecutorError("DEV authoring target is stale")
    P.load_development_resources()
    expected = P.resolve_dataverse_capabilities(
        row["component_type"], P.load_dataverse_capabilities()
    )
    if row["development_resources"].get("capabilities") != expected:
        raise ExecutorError("DEV capability snapshot is stale")
    return row, path


def capability_for(row: dict[str, Any], operation: str) -> dict[str, Any]:
    capability = (
        row["development_resources"].get("capabilities", {}).get("operations", {})
    ).get(operation)
    if not capability:
        raise ExecutorError(
            f"operation '{operation}' is not declared for {row['component_type']}"
        )
    if (
        capability.get("support") != "supported"
        or capability.get("primary_resource") != "dataverse-web-api"
        or not (capability.get("executor") or {}).get("supported")
    ):
        raise ExecutorError(
            f"{row['component_type']}.{operation} is not a compiler-approved Web API operation: "
            f"{capability.get('rationale')}",
            category="unsupported_operation",
        )
    return capability


def destructive_approval(row: dict[str, Any], operation: str) -> str:
    if operation not in {"delete", "remove_solution_component"}:
        return ""
    relationships = (row.get("payload") or {}).get("relationships")
    if (
        row.get("component_type") == "schema_relationship"
        and isinstance(relationships, list)
    ):
        names = ",".join(
            sorted(
                str(entry.get("schema_name") or entry.get("name") or "").strip()
                for entry in relationships
                if isinstance(entry, dict)
            )
        )
        if operation == "delete":
            return f"DELETE {row['id']} relationships={names}"
        solution = row["authoring_target"]["solution_unique_name"]
        return (
            f"REMOVE-SOLUTION-COMPONENT {row['id']} relationships={names} "
            f"solution={solution}"
        )
    if row.get("component_type") in ROW_COMPONENT_TYPES:
        name = row_component_name(row)
        if operation == "delete":
            return f"DELETE {row['id']} name={name}"
        solution = row["authoring_target"]["solution_unique_name"]
        return (
            f"REMOVE-SOLUTION-COMPONENT {row['id']} name={name} "
            f"solution={solution}"
        )
    field, identity = canonical_identity(row)
    if operation == "delete":
        return f"DELETE {row['id']} {field}={identity}"
    solution = row["authoring_target"]["solution_unique_name"]
    return (
        f"REMOVE-SOLUTION-COMPONENT {row['id']} {field}={identity} "
        f"solution={solution}"
    )


def validate_executor_preflight(
    row: dict[str, Any],
    operation: str,
    *,
    approval: str,
    check_oauth: bool,
) -> tuple[dict[str, Any], str, str]:
    capability = capability_for(row, operation)
    web_api_resource = next(
        (
            item
            for item in row["development_resources"].get("required") or []
            if item["id"] == "dataverse-web-api"
        ),
        None,
    )
    if not web_api_resource:
        raise ExecutorError(
            "Dataverse Web API is not a required compiler-owned resource"
        )
    service_root = str(web_api_resource.get("endpoint") or "")
    expected_service_root = (
        row["authoring_target"]["environment_url"].rstrip("/") + "/api/data/v9.2"
    )
    if service_root.rstrip("/") != expected_service_root:
        raise ExecutorError(
            "Dataverse Web API endpoint does not match the compiler-owned environment"
        )
    if row["implementation_scope"] != "repository_and_dataverse_solution":
        raise ExecutorError(
            "Web API executor is restricted to compiler-routed solution-aware components"
        )
    solution_name = str(
        row["authoring_target"].get("solution_unique_name") or ""
    )
    if (
        solution_name == P.UNRESOLVED_AUTHORING_VALUE
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,99}", solution_name)
    ):
        raise ExecutorError("compiler-owned solution unique name is missing or invalid")
    expected_approval = destructive_approval(row, operation)
    if expected_approval and approval != expected_approval:
        raise ExecutorError(
            "destructive approval is missing or does not exactly match: "
            + expected_approval,
            category="validation_error",
        )
    if check_oauth:
        validate_oauth_public_client(row)
    return capability, service_root, solution_name


def relationship_subrows(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand a grouped schema_relationship row into per-relationship sub-rows.

    A grouped component owns one `table` and a `relationships:` list, so all of a
    table's relationships compile into a single DEV task. Each sub-row is a fully
    formed row whose payload carries the shared owning fields plus one relationship
    entry, with the entry's own name promoted to the compiler-owned identity, so
    the existing per-relationship engine (identity, membership, verification)
    runs unchanged once per relationship. A flat component (or any non-relationship
    component) is returned unchanged as a single row.
    """
    payload = row.get("payload") or {}
    relationships = payload.get("relationships")
    if row.get("component_type") != "schema_relationship" or not isinstance(
        relationships, list
    ):
        return [row]
    if not relationships:
        raise ExecutorError(
            "grouped schema_relationship has no relationships to execute",
            category="validation_error",
        )
    subrows: list[dict[str, Any]] = []
    for entry in relationships:
        if not isinstance(entry, dict):
            raise ExecutorError(
                "grouped schema_relationship entry is not a mapping",
                category="validation_error",
            )
        sub_payload = {key: value for key, value in payload.items() if key != "relationships"}
        sub_payload.update(entry)
        identity = str(entry.get("schema_name") or entry.get("name") or "").strip()
        sub_payload["schema_name"] = identity
        sub_payload["record_name"] = identity
        sub = dict(row)
        sub["payload"] = sub_payload
        subrows.append(sub)
    return subrows


def _preflight_single(
    row: dict[str, Any],
    operation: str,
    *,
    approval: str,
) -> dict[str, Any]:
    capability, _, _ = validate_executor_preflight(
        row,
        operation,
        approval=approval,
        check_oauth=True,
    )
    if operation not in {"add_solution_component", "remove_solution_component"}:
        requests = build_static_requests(row, operation, capability)
        if not requests:
            raise ExecutorError("compiler-owned operation resolved to no requests")
        for request in requests:
            validate_capability_request(capability, request)
    return {
        "result": "ready",
        "operation": operation,
        "primary_resource": "dataverse-web-api",
        "action_invoked": False,
        "evidence_required": False,
    }


def preflight(
    row: dict[str, Any],
    operation: str,
    *,
    approval: str,
) -> dict[str, Any]:
    relationships = (row.get("payload") or {}).get("relationships")
    grouped = (
        row.get("component_type") == "schema_relationship"
        and isinstance(relationships, list)
    )
    if not grouped:
        return _preflight_single(row, operation, approval=approval)
    subrows = relationship_subrows(row)
    expected = destructive_approval(row, operation)
    if expected and approval != expected:
        raise ExecutorError(
            "destructive approval is missing or does not exactly match: " + expected,
            category="validation_error",
        )
    for sub in subrows:
        _preflight_single(
            sub,
            operation,
            approval=destructive_approval(sub, operation) if expected else approval,
        )
    return {
        "result": "ready",
        "operation": operation,
        "primary_resource": "dataverse-web-api",
        "action_invoked": False,
        "evidence_required": False,
    }


def authenticate_only(row: dict[str, Any]) -> dict[str, Any]:
    # Proves an in-memory MSAL session as gate evidence without any Dataverse request.
    validate_executor_preflight(row, "verify", approval="", check_oauth=True)
    acquire_token(row, row["authentication_policy"])
    return {
        "result": "authenticated",
        "environment_url": row["authoring_target"]["environment_url"].rstrip("/"),
        "solution_unique_name": row["authoring_target"]["solution_unique_name"],
        "action_invoked": False,
        "evidence_required": False,
    }


def row_lookup_request(row: dict[str, Any]) -> OperationRequest:
    component_type = row["component_type"]
    entity_set = ROW_ENTITY_SETS[component_type]
    id_field = ROW_ID_FIELDS[component_type]
    name = row_component_name(row)
    if component_type == "uiux_view":
        table = canonical_table(row["payload"].get("table"))
        filter_expr = (
            f"name eq '{odata_string(name)}' and "
            f"returnedtypecode eq '{odata_string(table)}' and "
            f"querytype eq {view_querytype(row['payload'])}"
        )
        select = f"{id_field},name,returnedtypecode,querytype"
    elif component_type == "sec_role":
        filter_expr = f"name eq '{odata_string(name)}'"
        select = f"{id_field},name"
    else:
        raise ExecutorError(
            f"executor has no whitelisted row lookup for {component_type}"
        )
    query = urlencode({"$select": select, "$filter": filter_expr})
    return OperationRequest(
        "GET",
        f"{entity_set}?{query}",
        None,
        (),
        (),
        "none",
        f"resolve exact {component_type} row ID",
    )


def record_lookup_request(row: dict[str, Any]) -> OperationRequest | None:
    if row["component_type"] in ROW_COMPONENT_TYPES:
        return row_lookup_request(row)
    _, identity = canonical_identity(row)
    if row["component_type"] == "config_env_variable":
        query = urlencode(
            {
                "$select": "environmentvariabledefinitionid,schemaname",
                "$filter": f"schemaname eq '{odata_string(identity)}'",
            }
        )
        return OperationRequest(
            "GET",
            "environmentvariabledefinitions?" + query,
            None,
            (),
            (),
            "none",
            "resolve exact environment-variable definition ID",
        )
    if row["component_type"] == "integ_connection_ref":
        query = urlencode(
            {
                "$select": "connectionreferenceid,connectionreferencelogicalname",
                "$filter": (
                    "connectionreferencelogicalname eq "
                    f"'{odata_string(identity)}'"
                ),
            }
        )
        return OperationRequest(
            "GET",
            "connectionreferences?" + query,
            None,
            (),
            (),
            "none",
            "resolve exact connection-reference ID",
        )
    return None


def resolve_table_view_context(
    row: dict[str, Any], client: DataverseClient
) -> tuple[str, str]:
    table = canonical_table(row["payload"].get("table"))
    result = client.request(
        OperationRequest(
            "GET",
            f"EntityDefinitions(LogicalName='{odata_string(table)}')?"
            + urlencode({"$select": "ObjectTypeCode,PrimaryIdAttribute"}),
            None,
            (),
            (),
            "none",
            "resolve exact table object type code and primary id for view layout",
        )
    )
    object_type_code = str(result.data.get("ObjectTypeCode") or "")
    primary_id_attribute = str(result.data.get("PrimaryIdAttribute") or "")
    if not re.fullmatch(r"-?\d+", object_type_code) or not re.fullmatch(
        r"[a-z][a-z0-9_]*", primary_id_attribute
    ):
        raise ExecutorError(
            "view parent table has no resolvable object type code",
            category="not_found",
        )
    return object_type_code, primary_id_attribute


def resolve_root_business_unit(client: DataverseClient) -> str:
    result = client.request(
        OperationRequest(
            "GET",
            "businessunits?"
            + urlencode(
                {
                    "$select": "businessunitid",
                    "$filter": "_parentbusinessunitid_value eq null",
                }
            ),
            None,
            (),
            (),
            "none",
            "resolve the root business unit that owns solution security roles",
        )
    )
    values = result.data.get("value") or []
    if len(values) != 1:
        raise ExecutorError(
            f"root business unit resolved to {len(values)} records",
            category="not_found" if not values else "conflict_or_duplicate",
        )
    value = str(values[0].get("businessunitid") or "")
    if not GUID_RE.fullmatch(value):
        raise ExecutorError("resolved business unit has no immutable ID")
    return value


def resolve_privilege_ids(
    row: dict[str, Any], client: DataverseClient
) -> list[dict[str, Any]]:
    privileges = (row.get("payload") or {}).get("privileges")
    if not isinstance(privileges, list) or not privileges:
        raise ExecutorError("security role payload requires at least one privilege")
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in privileges:
        if not isinstance(entry, dict):
            raise ExecutorError("security role privilege entries must be mappings")
        name = str(entry.get("privilege") or "").strip()
        if not PRIVILEGE_NAME_RE.fullmatch(name):
            raise ExecutorError("security role privilege name is not canonical")
        depth = PRIVILEGE_DEPTHS.get(str(entry.get("depth") or "").strip().lower())
        if depth is None:
            raise ExecutorError("security role privilege depth is not supported")
        if name in seen:
            raise ExecutorError("security role privileges must be unique")
        seen.add(name)
        result = client.request(
            OperationRequest(
                "GET",
                "privileges?"
                + urlencode(
                    {
                        "$select": "privilegeid,name",
                        "$filter": f"name eq '{odata_string(name)}'",
                    }
                ),
                None,
                (),
                (),
                "none",
                "resolve exact privilege ID for security-role assignment",
            )
        )
        values = result.data.get("value") or []
        if len(values) != 1:
            raise ExecutorError(
                f"privilege '{name}' resolved to {len(values)} definitions",
                category="not_found" if not values else "conflict_or_duplicate",
            )
        privilege_id = str(values[0].get("privilegeid") or "")
        if not GUID_RE.fullmatch(privilege_id):
            raise ExecutorError("resolved privilege has no immutable ID")
        resolved.append({"PrivilegeId": privilege_id, "Depth": depth})
    return resolved


def apply_role_privileges(
    row: dict[str, Any], client: DataverseClient, role_id: str
) -> OperationRequest:
    if not GUID_RE.fullmatch(role_id):
        raise ExecutorError("security-role privilege assignment needs a role ID")
    privileges = resolve_privilege_ids(row, client)
    request = OperationRequest(
        "POST",
        f"roles({role_id})/Microsoft.Dynamics.CRM.AddPrivilegesRole",
        {"Privileges": privileges},
        ("Privileges",),
        ("Privileges",),
        "none",
        "assign the compiler-declared privileges to the security role",
    )
    client.request(request)
    return request


def resolve_record_id(row: dict[str, Any], client: DataverseClient) -> str:
    request = record_lookup_request(row)
    if request is None:
        return ""
    result = client.request(request)
    values = result.data.get("value") or []
    if len(values) != 1:
        raise ExecutorError(
            f"canonical identity resolved to {len(values)} records",
            category="not_found" if not values else "conflict_or_duplicate",
        )
    key = (
        ROW_ID_FIELDS[row["component_type"]]
        if row["component_type"] in ROW_COMPONENT_TYPES
        else "environmentvariabledefinitionid"
        if row["component_type"] == "config_env_variable"
        else "connectionreferenceid"
    )
    value = str(values[0].get(key) or "")
    if not GUID_RE.fullmatch(value):
        raise ExecutorError("resolved record has no immutable ID")
    return value


def resolve_metadata_id(row: dict[str, Any], client: DataverseClient) -> str:
    payload = row["payload"]
    _, identity = canonical_identity(row)
    if row["component_type"] == "schema_relationship":
        path = (
            "RelationshipDefinitions?"
            + urlencode(
                {
                    "$select": "MetadataId,SchemaName",
                    "$filter": f"SchemaName eq '{odata_string(identity)}'",
                }
            )
        )
    elif row["component_type"] == "schema_key":
        table = canonical_table(payload.get("table"))
        path = (
            f"EntityDefinitions(LogicalName='{odata_string(table)}')/Keys?"
            + urlencode({"$select": "MetadataId,SchemaName"})
        )
    else:
        return ""
    result = client.request(
        OperationRequest(
            "GET", path, None, (), (), "none", "resolve exact metadata ID"
        )
    )
    values = result.data.get("value") or []
    matches = [
        item
        for item in values
        if str(item.get("SchemaName") or "").lower() == identity.lower()
    ]
    if len(matches) != 1:
        raise ExecutorError(
            f"canonical metadata identity resolved to {len(matches)} definitions",
            category="not_found" if not matches else "conflict_or_duplicate",
        )
    value = str(matches[0].get("MetadataId") or "")
    if not GUID_RE.fullmatch(value):
        raise ExecutorError("resolved metadata has no immutable ID")
    return value


def extract_immutable_id(result: HttpResult) -> str:
    match = GUID_RE.search(result.entity_id)
    return match.group(0) if match else ""


def response_item(row: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    values = data.get("value")
    if not isinstance(values, list):
        return data
    if row["component_type"] in ROW_COMPONENT_TYPES:
        name = row_component_name(row).lower()
        matches = [
            item
            for item in values
            if str(item.get("name") or "").lower() == name
        ]
        return matches[0] if len(matches) == 1 else {}
    _, identity = canonical_identity(row)
    identity_fields = {
        "schema_key": "SchemaName",
        "config_env_variable": "schemaname",
        "integ_connection_ref": "connectionreferencelogicalname",
    }
    field = identity_fields.get(row["component_type"])
    if not field:
        return values[0] if len(values) == 1 else {}
    matches = [
        item
        for item in values
        if str(item.get(field) or "").lower() == identity.lower()
    ]
    return matches[0] if len(matches) == 1 else {}


def response_immutable_id(row: dict[str, Any], data: dict[str, Any]) -> str:
    item = response_item(row, data)
    keys = {
        "config_env_variable": "environmentvariabledefinitionid",
        "integ_connection_ref": "connectionreferenceid",
        **ROW_ID_FIELDS,
    }
    value = str(item.get(keys.get(row["component_type"], "MetadataId")) or "")
    return value if GUID_RE.fullmatch(value) else ""


def membership_object_id(
    row: dict[str, Any], client: DataverseClient, fallback_id: str
) -> str:
    """Resolve the component-specific ID used by solutioncomponents.objectid."""
    return fallback_id


def resolve_component_object_id(
    row: dict[str, Any], client: DataverseClient
) -> str:
    if row["component_type"] == "schema_relationship":
        return resolve_metadata_id(row, client)
    result = client.request(verification_request(row))
    object_id = response_immutable_id(row, result.data)
    if not object_id:
        raise ExecutorError(
            "canonical component identity has no resolvable immutable object ID",
            category="not_found",
        )
    return object_id


def resolve_solution_id(row: dict[str, Any], client: DataverseClient) -> str:
    solution_name = row["authoring_target"]["solution_unique_name"]
    path = "solutions?" + urlencode(
        {
            "$select": "solutionid,uniquename",
            "$filter": f"uniquename eq '{odata_string(solution_name)}'",
        }
    )
    values = client.request(
        OperationRequest(
            "GET", path, None, (), (), "none", "resolve exact routed solution ID"
        )
    ).data.get("value") or []
    if len(values) != 1:
        raise ExecutorError(
            f"routed solution identity resolved to {len(values)} rows",
            category="not_found" if not values else "conflict_or_duplicate",
        )
    value = str(values[0].get("solutionid") or "")
    if not GUID_RE.fullmatch(value):
        raise ExecutorError("routed solution has no immutable ID")
    return value


def declared_solution_ids(client: DataverseClient) -> dict[str, str]:
    contract = P.load_authoring_targets()
    result: dict[str, str] = {}
    for solution in (contract.get("solutions") or {}).values():
        unique_name = str(solution.get("unique_name") or "")
        if not unique_name or unique_name in result:
            continue
        path = "solutions?" + urlencode(
            {
                "$select": "solutionid,uniquename",
                "$filter": f"uniquename eq '{odata_string(unique_name)}'",
            }
        )
        values = client.request(
            OperationRequest(
                "GET",
                path,
                None,
                (),
                (),
                "none",
                "resolve declared custom solution ID for targeted membership verification",
            )
        ).data.get("value") or []
        if len(values) == 1 and GUID_RE.fullmatch(
            str(values[0].get("solutionid") or "")
        ):
            result[unique_name] = str(values[0]["solutionid"])
    return result


def solution_component_rows(
    client: DataverseClient,
    object_id: str,
    *,
    solution_id: str = "",
) -> list[dict[str, Any]]:
    filters = [f"objectid eq {object_id}"]
    if solution_id:
        filters.append(f"_solutionid_value eq {solution_id}")
    path = "solutioncomponents?" + urlencode(
        {
            "$select": (
                "solutioncomponentid,objectid,componenttype,"
                "rootcomponentbehavior,_solutionid_value"
            ),
            "$filter": " and ".join(filters),
        }
    )
    values = client.request(
        OperationRequest(
            "GET",
            path,
            None,
            (),
            (),
            "none",
            "read exact solution-component membership",
        )
    ).data.get("value") or []
    return [item for item in values if isinstance(item, dict)]


def effective_solution_component_rows(
    row: dict[str, Any],
    client: DataverseClient,
    object_id: str,
    *,
    solution_id: str = "",
) -> list[dict[str, Any]]:
    rows = solution_component_rows(client, object_id, solution_id=solution_id)
    if row["component_type"] not in {"schema_relationship", "schema_key"}:
        return rows
    table = canonical_table(row["payload"].get("table"))
    table_result = client.request(
        OperationRequest(
            "GET",
            f"EntityDefinitions(LogicalName='{odata_string(table)}')",
            None,
            (),
            (),
            "none",
            "resolve exact parent table metadata for inherited membership",
        )
    )
    table_id = str(table_result.data.get("MetadataId") or "")
    if not GUID_RE.fullmatch(table_id):
        raise ExecutorError("parent table has no metadata ID")
    inherited = solution_component_rows(
        client, table_id, solution_id=solution_id
    )
    rows.extend(
        item
        for item in inherited
        if item.get("componenttype") == 1
        and item.get("rootcomponentbehavior") == 0
    )
    return rows


def solution_action_request(
    row: dict[str, Any],
    operation: str,
    client: DataverseClient,
) -> tuple[OperationRequest, str]:
    solution_id = resolve_solution_id(row, client)
    solution_name = row["authoring_target"]["solution_unique_name"]
    if operation == "add_solution_component":
        if row["component_type"] == "schema_relationship":
            object_id = resolve_metadata_id(row, client)
            component_type = 10
        else:
            object_id = resolve_component_object_id(row, client)
            rows = solution_component_rows(client, object_id)
            component_types = {
                int(item["componenttype"])
                for item in rows
                if isinstance(item.get("componenttype"), int)
            }
            if len(component_types) != 1:
                raise ExecutorError(
                    "existing component type could not be resolved unambiguously",
                    category="unsupported_operation",
                )
            component_type = component_types.pop()
        body = {
            "ComponentId": object_id,
            "ComponentType": component_type,
            "SolutionUniqueName": solution_name,
            "AddRequiredComponents": False,
            "DoNotIncludeSubcomponents": True,
        }
        return (
            OperationRequest(
                "POST",
                "AddSolutionComponent",
                body,
                tuple(body),
                ("solution_membership",),
                "action_parameter",
                "add exact existing component to routed solution",
            ),
            object_id,
        )
    object_id = resolve_component_object_id(row, client)
    memberships = solution_component_rows(
        client, object_id, solution_id=solution_id
    )
    if len(memberships) != 1:
        raise ExecutorError(
            f"routed solution membership resolved to {len(memberships)} rows",
            category="not_found" if not memberships else "conflict_or_duplicate",
        )
    membership = memberships[0]
    component_type = membership.get("componenttype")
    membership_id = str(membership.get("solutioncomponentid") or "")
    if not isinstance(component_type, int) or not GUID_RE.fullmatch(membership_id):
        raise ExecutorError("routed solution membership is incomplete")
    body = {
        "SolutionComponent": {
            "@odata.type": "Microsoft.Dynamics.CRM.solutioncomponent",
            "solutioncomponentid": membership_id,
        },
        "ComponentType": component_type,
        "SolutionUniqueName": solution_name,
    }
    return (
        OperationRequest(
            "POST",
            "RemoveSolutionComponent",
            body,
            ("SolutionComponent", "ComponentType", "SolutionUniqueName"),
            ("solution_membership",),
            "action_parameter",
            "remove exact component membership from routed solution",
        ),
        object_id,
    )


def verification_request(
    row: dict[str, Any], request: OperationRequest | None = None
) -> OperationRequest:
    if (
        request
        and row["component_type"] == "schema_table"
        and request.path.endswith("/Attributes")
        and isinstance(request.body, dict)
    ):
        table = canonical_table(row["payload"].get("table"))
        column = str(request.body.get("SchemaName") or "").lower()
        return OperationRequest(
            "GET",
            f"EntityDefinitions(LogicalName='{odata_string(table)}')/"
            f"Attributes(LogicalName='{odata_string(column)}')",
            None,
            (),
            (),
            "none",
            "verify exact child column added to existing table",
        )
    capability = capability_for(row, "verify")
    return build_static_requests(row, "verify", capability)[0]


def expected_payload_matches(
    row: dict[str, Any],
    request: OperationRequest,
    item: dict[str, Any],
) -> bool:
    if not item:
        return False
    body = request.body or {}
    component_type = row["component_type"]
    if component_type in ROW_COMPONENT_TYPES:
        name = row_component_name(row).lower()
        return str(item.get("name") or "").lower() == name
    if component_type in {"config_env_variable", "integ_connection_ref"}:
        for key, value in body.items():
            if key not in item:
                continue
            if str(item[key]) != str(value):
                return False
        return True
    expected_schema = str(body.get("SchemaName") or "")
    if expected_schema:
        actual_schema = str(item.get("SchemaName") or item.get("LogicalName") or "")
        if actual_schema.lower() != expected_schema.lower():
            return False
    if component_type == "schema_key" and body.get("KeyAttributes"):
        if sorted(item.get("KeyAttributes") or []) != sorted(body["KeyAttributes"]):
            return False
    if component_type == "schema_choice" and request.path == "UpdateOptionValue":
        value = body.get("Value")
        matches = [
            option
            for option in item.get("Options") or []
            if option.get("Value") == value
        ]
        return len(matches) == 1
    if component_type == "schema_choice" and body.get("Name"):
        if str(item.get("Name") or "").lower() != str(body["Name"]).lower():
            return False
        expected_values = sorted(
            option.get("Value") for option in body.get("Options") or []
        )
        if expected_values:
            actual_values = sorted(
                option.get("Value") for option in item.get("Options") or []
            )
            if actual_values != expected_values:
                return False
    return True


def guard_get_only_guid_path(method: str, path: str) -> None:
    """A verification path built from a caller-supplied immutable ID must stay
    GET-only, so a recovery identifier can never be routed into a write."""
    if not GUID_RE.search(path):
        raise ExecutorError(
            "verification path carries no immutable ID to validate",
            category="validation_error",
        )
    if method != "GET":
        raise ExecutorError(
            f"{method} is outside the read-only GUID verification whitelist",
            category="unsupported_operation",
        )
    validate_runtime_request(method, path)


def metadata_verification_request(
    row: dict[str, Any], immutable_id: str
) -> OperationRequest:
    """Verify a relationship by its immutable MetadataId rather than a mutable
    SchemaName, confirming the identifier the create actually returned."""
    if row["component_type"] != "schema_relationship":
        raise ExecutorError(
            "immutable-ID verification is limited to schema_relationship",
            category="unsupported_operation",
        )
    if not GUID_RE.fullmatch(immutable_id):
        raise ExecutorError(
            "immutable MetadataId is not a canonical GUID",
            category="validation_error",
        )
    path = f"RelationshipDefinitions({immutable_id})"
    guard_get_only_guid_path("GET", path)
    return OperationRequest(
        "GET",
        path,
        None,
        (),
        (),
        "none",
        "verify relationship by immutable MetadataId",
    )


def verify_result(
    row: dict[str, Any],
    client: DataverseClient,
    immutable_id: str,
    *,
    deleted: bool,
    membership_removed: bool = False,
    request: OperationRequest | None = None,
) -> tuple[dict[str, str], str]:
    try:
        if row["component_type"] == "schema_relationship" and GUID_RE.fullmatch(
            immutable_id
        ):
            verify_request = metadata_verification_request(row, immutable_id)
        else:
            verify_request = verification_request(row, request)
        result = client.request(verify_request)
        item = response_item(row, result.data)
        found = bool(item)
    except ExecutorError as exc:
        if deleted and exc.status == 404:
            return (
                {
                    "identity": "matched",
                    "payload": "not-applicable",
                    "membership": "not-applicable",
                },
                "",
            )
        raise
    if deleted:
        if found:
            raise ExecutorError(
                "targeted verification still found the deleted identity",
                category="verification_mismatch",
            )
        return (
            {
                "identity": "matched",
                "payload": "not-applicable",
                "membership": "not-applicable",
            },
            immutable_id,
        )
    if not found:
        raise ExecutorError(
            "targeted verification did not find the canonical identity",
            category="verification_mismatch",
        )
    immutable_id = immutable_id or response_immutable_id(row, result.data)
    if not immutable_id:
        raise ExecutorError(
            "targeted verification returned no immutable ID",
            category="verification_mismatch",
        )
    payload_matched = expected_payload_matches(
        row,
        request
        or OperationRequest(
            "GET", "", {}, (), (), "none", "verify compiler-owned payload"
        ),
        item,
    )
    if not payload_matched:
        raise ExecutorError(
            "targeted verification found a payload mismatch",
            category="verification_mismatch",
        )
    membership = "not-applicable"
    if row["implementation_scope"] == "repository_and_dataverse_solution":
        solution_id = resolve_solution_id(row, client)
        object_id = membership_object_id(row, client, immutable_id)
        memberships = effective_solution_component_rows(
            row,
            client, object_id, solution_id=solution_id
        )
        routed_membership = any(
            str(item.get("_solutionid_value") or "") == solution_id
            for item in memberships
        )
        if routed_membership == membership_removed:
            raise ExecutorError(
                "targeted routed-solution membership verification failed",
                category="verification_mismatch",
            )
        if not membership_removed:
            declared_ids = set(declared_solution_ids(client).values())
            all_memberships = effective_solution_component_rows(
                row, client, object_id
            )
            actual_declared_ids = {
                str(item.get("_solutionid_value") or "")
                for item in all_memberships
                if str(item.get("_solutionid_value") or "") in declared_ids
            }
            if actual_declared_ids != {solution_id}:
                raise ExecutorError(
                    "component membership does not resolve only to the routed "
                    "solution among declared custom solutions",
                    category="verification_mismatch",
                )
        membership = "matched"
    return (
        {
            "identity": "matched",
            "payload": "matched",
            "membership": membership,
        },
        immutable_id,
    )


def remediation(category: str) -> str:
    return {
        "unauthenticated": "Reauthenticate the current human against the exact compiler-owned environment.",
        "permission_or_client_allowlist": "An administrator must approve the public client or grant the current user the required Dataverse privilege.",
        "not_found": "Correct or restore the compiler-owned environment, solution, record, or component prerequisite; do not infer another target.",
        "conflict_or_duplicate": "Resolve the exact canonical identity conflict without renaming or repairing automatically.",
        "unsupported_operation": "Use only the capability matrix's permitted primary or evidence-gated fallback route.",
        "configuration_prerequisite": "Configure the customer-owned non-secret public-client ID and tenant, delegated Dataverse permission, and exact pinned MSAL dependency.",
        "validation_error": "Correct the DEV payload or authoritative upstream artifact.",
        "verification_mismatch": "Review the exact identity, payload, and solution membership mismatch; do not repair automatically.",
        "unavailable": "Restore the exact routed API endpoint or retry the idempotent read after service recovery.",
    }.get(category, "Review the sanitized platform error and apply the owning remediation.")


def evidence_payload(
    row: dict[str, Any],
    issue_number: int,
    operation: str,
    request: OperationRequest,
    *,
    result: str,
    status: str,
    error_code: str,
    message: str,
    immutable_id: str,
    correlation_id: str,
    verification: dict[str, str],
    write_occurred: bool,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    attempt = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    field, identity = canonical_identity(row)
    return {
        "schema_version": 1,
        "attempt_id": attempt,
        "timestamp_utc": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "issue_number": issue_number,
        "result": result,
        "dev_id": row["id"],
        "component_id": row["component"],
        "component_type": row["component_type"],
        "build_skill": row["build_skill"],
        "task_context_hash": row["task_context_hash"],
        "source_plan_hash": row["source_plan_hash"],
        "operation": {
            "resource": "dataverse-web-api",
            "server": "not-applicable",
            "tool": "dataverse_web_api_executor",
            "api_operation": request.description,
        },
        "target": {
            "scope": row["implementation_scope"],
            "environment_url": row["authoring_target"]["environment_url"],
            "solution_or_record": row["authoring_target"].get(
                "solution_unique_name", identity
            ),
            "identity_field": field,
            "identity_value": identity,
        },
        "request": {
            "operation": request.description,
            "parameter_names": list(request.parameter_names),
            "identifiers": {
                field: identity,
                "solution_unique_name": row["authoring_target"].get(
                    "solution_unique_name", "not-applicable"
                ),
            },
        },
        "response": {
            "status": status,
            "error_code": error_code,
            "message": sanitize_text(message),
            "immutable_id": immutable_id,
            "changed_fields": list(request.changed_fields) if write_occurred else [],
            "verified_fields": (
                list(request.changed_fields) if result == "succeeded" else []
            ),
            "correlation_id": correlation_id,
            "details_withheld": True,
        },
        "verification": verification,
        "remediation": remediation(error_code),
        "write_occurred": write_occurred,
        "further_writes_stopped": result != "succeeded",
    }


def post_evidence(payload: dict[str, Any], issue_number: int) -> dict[str, Any]:
    import execution_evidence

    validated = execution_evidence.validate_payload(payload)
    return execution_evidence.post_evidence(
        validated, issue_number=issue_number, repo=None
    )


def execute(
    row: dict[str, Any],
    operation: str,
    issue_number: int,
    *,
    approval: str,
    dry_run: bool,
    verification_id: str = "",
) -> list[dict[str, Any]]:
    """Execute a compiler-approved operation for one DEV task.

    A grouped schema_relationship task fans out into one per-relationship sub-row
    and runs the per-relationship engine once each, aggregating results and
    evidence under the single DEV task. A single human destructive approval
    (listing every relationship) authorizes the whole grouped task. A flat or
    non-relationship task executes exactly as before.
    """
    relationships = (row.get("payload") or {}).get("relationships")
    grouped = (
        row.get("component_type") == "schema_relationship"
        and isinstance(relationships, list)
    )
    if not grouped:
        return _execute_single(
            row,
            operation,
            issue_number,
            approval=approval,
            dry_run=dry_run,
            verification_id=verification_id,
        )
    if verification_id:
        raise ExecutorError(
            "verification-id recovery is limited to a single relationship",
            category="unsupported_operation",
        )
    subrows = relationship_subrows(row)
    expected = destructive_approval(row, operation)
    if expected and approval != expected:
        raise ExecutorError(
            "destructive approval is missing or does not exactly match: " + expected,
            category="validation_error",
        )
    results: list[dict[str, Any]] = []
    for sub in subrows:
        results.extend(
            _execute_single(
                sub,
                operation,
                issue_number,
                approval=destructive_approval(sub, operation) if expected else approval,
                dry_run=dry_run,
            )
        )
    return results


def _execute_single(
    row: dict[str, Any],
    operation: str,
    issue_number: int,
    *,
    approval: str,
    dry_run: bool,
    verification_id: str = "",
) -> list[dict[str, Any]]:
    capability, service_root, solution_name = validate_executor_preflight(
        row,
        operation,
        approval=approval,
        check_oauth=not dry_run,
    )
    if verification_id:
        if operation != "verify":
            raise ExecutorError(
                "verification-id recovery requires the verify operation",
                category="unsupported_operation",
            )
        if row["component_type"] != "schema_relationship":
            raise ExecutorError(
                "verification-id recovery is limited to schema_relationship",
                category="unsupported_operation",
            )
        if not GUID_RE.fullmatch(verification_id):
            raise ExecutorError(
                "verification-id must be an immutable MetadataId GUID",
                category="validation_error",
            )
        if dry_run:
            return [
                {
                    "method": "GET",
                    "endpoint_family": capability["http"]["endpoint_family"],
                    "path_template": (
                        "RelationshipDefinitions({metadata_id})"
                    ),
                    "solution_context": "none",
                    "description": (
                        "verify-only recovery re-checks identity and routed "
                        "membership by immutable MetadataId without writing"
                    ),
                    "parameter_names": [],
                    "body_withheld": False,
                }
            ]
    if dry_run:
        if operation in {"add_solution_component", "remove_solution_component"}:
            return [
                {
                    "method": capability["http"]["method"],
                    "endpoint_family": capability["http"]["endpoint_family"],
                    "path_template": capability["http"]["path_template"],
                    "solution_context": capability["solution_context"]["mechanism"],
                    "description": (
                        "resolve the canonical immutable component ID and exact "
                        "solutioncomponent membership before invoking the action"
                    ),
                    "parameter_names": (
                        [
                            "ComponentId",
                            "ComponentType",
                            "SolutionUniqueName",
                            "AddRequiredComponents",
                            "DoNotIncludeSubcomponents",
                        ]
                        if operation == "add_solution_component"
                        else [
                            "SolutionComponent",
                            "ComponentType",
                            "SolutionUniqueName",
                        ]
                    ),
                    "body_withheld": True,
                }
            ]
        requests = build_static_requests(row, operation, capability)
        for request in requests:
            validate_capability_request(capability, request)
        return [
            {
                "method": request.method,
                "endpoint_family": capability["http"]["endpoint_family"],
                "path_template": capability["http"]["path_template"],
                "solution_context": capability["solution_context"]["mechanism"],
                "description": request.description,
                "parameter_names": list(request.parameter_names),
                "body_withheld": request.body is not None,
            }
            for request in requests
        ]
    mechanism = capability["solution_context"]["mechanism"]
    request = OperationRequest(
        capability["http"]["method"],
        capability["http"]["path_template"],
        None,
        (),
        (),
        (
            "header"
            if mechanism == "MSCRM.SolutionUniqueName"
            else "action_parameter"
            if mechanism == "action_parameter"
            else "none"
        ),
        f"{row['component_type']}.{operation} compiler-approved operation",
    )
    client: DataverseClient | None = None
    write_completed = False
    write_immutable_id = ""
    write_correlation_id = ""
    try:
        token = acquire_token(row, row["authentication_policy"])
        client = DataverseClient(service_root, token, solution_name)
        if verification_id:
            request = metadata_verification_request(row, verification_id)
            verification, immutable_id = verify_result(
                row,
                client,
                verification_id,
                deleted=False,
                membership_removed=False,
                request=request,
            )
            evidence = evidence_payload(
                row,
                issue_number,
                operation,
                request,
                result="succeeded",
                status="200",
                error_code="no_error",
                message=(
                    "Verify-only recovery reconciled the captured immutable "
                    "MetadataId without writing."
                ),
                immutable_id=immutable_id,
                correlation_id="",
                verification=verification,
                write_occurred=False,
            )
            posted = post_evidence(evidence, issue_number)
            return [
                {
                    "result": "succeeded",
                    "status": 200,
                    "operation": request.description,
                    "immutable_id": immutable_id,
                    "evidence": posted.get("result"),
                }
            ]
        resolved_id = ""
        metadata_id = ""
        if operation in {"update", "delete"}:
            resolved_id = resolve_record_id(row, client)
            if row["component_type"] == "schema_relationship":
                metadata_id = resolve_metadata_id(row, client)
        object_type_code = ""
        primary_id_attribute = ""
        if row["component_type"] == "uiux_view" and operation in {
            "create",
            "update",
        }:
            object_type_code, primary_id_attribute = resolve_table_view_context(
                row, client
            )
        business_unit_id = ""
        if row["component_type"] == "sec_role" and operation == "create":
            business_unit_id = resolve_root_business_unit(client)
        action_object_id = ""
        if operation in {"add_solution_component", "remove_solution_component"}:
            action_request, action_object_id = solution_action_request(
                row, operation, client
            )
            requests = [action_request]
        else:
            requests = build_static_requests(
                row,
                operation,
                capability,
                resolved_id=resolved_id or "{record_id}",
                metadata_id=metadata_id or "{metadata_id}",
                object_type_code=object_type_code or "{object_type_code}",
                primary_id_attribute=primary_id_attribute
                or "{primary_id_attribute}",
                business_unit_id=business_unit_id or "{business_unit_id}",
            )
        if not requests:
            raise ExecutorError("compiler-owned operation resolved to no requests")
        outputs = []
        for raw_request in requests:
            validate_capability_request(capability, raw_request)
            request = merge_metadata_update(row, raw_request, client)
            http_result = client.request(request)
            immutable_id = (
                extract_immutable_id(http_result)
                or resolved_id
                or metadata_id
                or action_object_id
            )
            if request.method in WRITE_METHODS:
                write_completed = True
                write_immutable_id = immutable_id
                write_correlation_id = http_result.correlation_id
            verification, immutable_id = verify_result(
                row,
                client,
                immutable_id,
                deleted=operation == "delete",
                membership_removed=operation == "remove_solution_component",
                request=request,
            )
            evidence = evidence_payload(
                row,
                issue_number,
                operation,
                request,
                result="succeeded",
                status=str(http_result.status),
                error_code="no_error",
                message="Scoped operation and targeted verification succeeded.",
                immutable_id=immutable_id,
                correlation_id=http_result.correlation_id,
                verification=verification,
                write_occurred=request.method in WRITE_METHODS,
            )
            posted = post_evidence(evidence, issue_number)
            outputs.append(
                {
                    "result": "succeeded",
                    "status": http_result.status,
                    "operation": request.description,
                    "immutable_id": immutable_id,
                    "evidence": posted.get("result"),
                }
            )
        if row["component_type"] == "sec_role" and operation in {
            "create",
            "update",
        }:
            role_id = write_immutable_id or resolved_id
            privilege_request = apply_role_privileges(row, client, role_id)
            evidence = evidence_payload(
                row,
                issue_number,
                operation,
                privilege_request,
                result="succeeded",
                status="204",
                error_code="no_error",
                message=(
                    "AddPrivilegesRole assigned the declared privileges at the "
                    "requested depths."
                ),
                immutable_id=role_id,
                correlation_id="",
                verification={
                    "identity": "matched",
                    "payload": "matched",
                    "membership": "not-applicable",
                },
                write_occurred=True,
            )
            posted = post_evidence(evidence, issue_number)
            outputs.append(
                {
                    "result": "succeeded",
                    "status": 204,
                    "operation": privilege_request.description,
                    "immutable_id": role_id,
                    "evidence": posted.get("result"),
                }
            )
        return outputs
    except ExecutorError as exc:
        exc.action_invoked = bool(client and client.request_count)
        if not exc.action_invoked:
            raise
        evidence = evidence_payload(
            row,
            issue_number,
            operation,
            request,
            result="blocked",
            status=str(exc.status or "blocked"),
            error_code=exc.category,
            message=str(exc),
            immutable_id=write_immutable_id if write_completed else "",
            correlation_id=(
                write_correlation_id if write_completed else exc.correlation_id
            ),
            verification={
                "identity": "not-run",
                "payload": "not-run",
                "membership": "not-run",
            },
            write_occurred=write_completed,
        )
        post_evidence(evidence, issue_number)
        exc.evidence_posted = True
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dev_id")
    parser.add_argument(
        "--operation",
        required=True,
        choices=[
            "create",
            "update",
            "delete",
            "verify",
            "publish",
            "add_solution_component",
            "remove_solution_component",
        ],
    )
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--approve-destructive", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--authenticate-only", action="store_true")
    parser.add_argument("--verification-id", default="")
    args = parser.parse_args()
    try:
        row, _ = load_row(args.dev_id)
        if args.authenticate_only:
            output = authenticate_only(row)
        elif args.preflight_only:
            output = preflight(
                row,
                args.operation,
                approval=args.approve_destructive,
            )
        else:
            output = execute(
                row,
                args.operation,
                args.issue_number,
                approval=args.approve_destructive,
                dry_run=args.dry_run,
                verification_id=args.verification_id,
            )
    except (P.PipelineError, ExecutorError) as exc:
        print(
            json.dumps(
                {
                    "result": "blocked",
                    "category": getattr(exc, "category", "validation_error"),
                    "message": sanitize_text(exc),
                    "action_invoked": getattr(exc, "action_invoked", False),
                    "evidence_required": getattr(exc, "action_invoked", False),
                    "evidence_posted": getattr(exc, "evidence_posted", False),
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps({"result": "succeeded", "operations": output}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
