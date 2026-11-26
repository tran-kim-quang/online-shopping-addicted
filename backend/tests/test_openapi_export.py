"""``scripts/export_openapi.py`` must reproduce the committed ``backend/openapi.json``.

The frontend generates its typed client from that file (``npm run gen:api`` →
``lib/api/schema.d.ts``), so a stale export silently breaks the web app at build time.
These tests also pin the Phase 5 additions the web client depends on: the
``GET /api/sources`` query contract, ``display_name``/``last_job_id`` on source payloads,
and ``ReportWarning.params``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
COMMITTED_SCHEMA = BACKEND_DIR / "openapi.json"
EXPORT_SCRIPT = BACKEND_DIR / "scripts" / "export_openapi.py"


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_committed_openapi_json_is_committed_and_parsable() -> None:
    assert COMMITTED_SCHEMA.is_file(), "backend/openapi.json must exist (frontend codegen input)"
    schema = _load(COMMITTED_SCHEMA)
    assert schema["openapi"].startswith("3.1")
    assert "/api/sources" in schema["paths"]


def test_export_matches_committed_file_byte_for_byte(tmp_path: Path) -> None:
    from scripts.export_openapi import export_openapi

    target = export_openapi(tmp_path / "openapi.json")

    assert target.read_text(encoding="utf-8") == COMMITTED_SCHEMA.read_text(encoding="utf-8"), (
        "backend/openapi.json is stale — re-run `uv run python scripts/export_openapi.py`"
    )


def test_export_is_deterministic_across_runs(tmp_path: Path) -> None:
    from scripts.export_openapi import export_openapi

    first = export_openapi(tmp_path / "a.json").read_text(encoding="utf-8")
    second = export_openapi(tmp_path / "b.json").read_text(encoding="utf-8")

    assert first == second


def test_cli_entrypoint_writes_the_schema(tmp_path: Path) -> None:
    target = tmp_path / "cli.json"

    result = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), str(target)],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert f"Exported OpenAPI schema to {target}" in result.stdout
    assert _load(target) == _load(COMMITTED_SCHEMA)


def test_sources_list_contract_exposes_pagination_and_filters() -> None:
    schema = _load(COMMITTED_SCHEMA)
    operation = schema["paths"]["/api/sources"]["get"]
    params = {item["name"]: item for item in operation["parameters"]}

    assert set(params) == {"limit", "offset", "status", "url_type", "platform", "fresh"}
    limit_schema = params["limit"]["schema"]
    assert (limit_schema["default"], limit_schema["minimum"], limit_schema["maximum"]) == (
        20,
        1,
        100,
    )
    assert params["offset"]["schema"]["default"] == 0
    # Vòng 2 (cache-policy): "Gần đây" asks for the cache-fresh subset only.
    assert params["fresh"]["schema"]["type"] == "boolean"
    assert params["fresh"]["schema"]["default"] is False
    assert params["fresh"]["required"] is False

    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert response_ref.endswith("/SourceListResponse")


def test_source_list_items_expose_the_cache_window() -> None:
    """Vòng 2: every list item carries ``refresh_after`` (null when it was never crawled)."""
    properties = _load(COMMITTED_SCHEMA)["components"]["schemas"]["SourceListItem"]["properties"]

    assert properties["refresh_after"]["anyOf"] == [
        {"format": "date-time", "type": "string"},
        {"type": "null"},
    ]


def test_source_payloads_expose_display_name_and_last_job_id() -> None:
    components = _load(COMMITTED_SCHEMA)["components"]["schemas"]

    for name in ("SourceListItem", "SourceSummaryResponse"):
        properties = components[name]["properties"]
        assert "display_name" in properties, name
        assert "last_job_id" in properties, name
        assert properties["last_job_id"]["anyOf"][0]["type"] == "string"

    list_item = components["SourceListItem"]["properties"]
    assert {
        "products_count",
        "reviews_collected",
        "chat_sessions_count",
        "ingested",
        "crawled_at",
        "updated_at",
    } <= set(list_item)


def test_warning_params_are_typed_as_json_scalars() -> None:
    components = _load(COMMITTED_SCHEMA)["components"]["schemas"]
    params = components["ReportWarning"]["properties"]["params"]

    assert params["type"] == "object"
    allowed = {item["type"] for item in params["additionalProperties"]["anyOf"]}
    assert allowed == {"integer", "number", "string"}


def test_report_product_row_exposes_image_and_short_description() -> None:
    components = _load(COMMITTED_SCHEMA)["components"]["schemas"]

    properties = components["ProductReportRow"]["properties"]

    assert "image_url" in properties
    assert "short_description" in properties
