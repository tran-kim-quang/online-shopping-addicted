"""Export FastAPI OpenAPI schema to ``backend/openapi.json`` without requiring a DB."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.main import create_app  # noqa: E402


def export_openapi(output_path: Path | None = None) -> Path:
    """Serialize ``create_app().openapi()`` as deterministic formatted JSON."""
    target = output_path if output_path is not None else BACKEND_ROOT / "openapi.json"
    schema = create_app().openapi()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def main() -> None:
    out_arg = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    written = export_openapi(out_arg)
    print(f"Exported OpenAPI schema to {written}")


if __name__ == "__main__":
    main()
