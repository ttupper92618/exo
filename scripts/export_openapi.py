from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

os.environ.setdefault("SKULK_HOME", ".skulk-docs-home")

from skulk.api.main import API
from skulk.api.operator_auth import create_operator_auth_router
from skulk.operator.pairing import OperatorPairingService
from skulk.shared.types.common import NodeId
from skulk.utils.channels import channel

REPO_ROOT = Path(__file__).resolve().parent.parent
REDOC_BUNDLE_SOURCE = (
    REPO_ROOT
    / "dashboard-react"
    / "node_modules"
    / "redoc"
    / "bundles"
    / "redoc.standalone.js"
)


def build_docs_api() -> API:
    """Describe all HTTP contracts without initializing an operator authority."""
    command_sender, _ = channel()
    download_sender, _ = channel()
    _, event_receiver = channel()
    _, election_receiver = channel()

    api = API(
        NodeId("docs-node"),
        port=52415,
        event_receiver=event_receiver,
        command_sender=command_sender,
        download_command_sender=download_sender,
        election_receiver=election_receiver,
        enable_event_log=False,
        mount_dashboard=False,
    )
    # The gateway is optional at runtime, but its routes must still appear in
    # published API documentation. Router construction does not read credentials
    # or initialize keys; no requests are dispatched by this exporter.
    api.app.include_router(
        create_operator_auth_router(OperatorPairingService.from_default_paths())
    )
    return api


def _hoist_defs(schema: dict) -> dict:
    """Move inline ``$defs`` up to ``components/schemas`` and rewrite ``$ref`` pointers.

    Pydantic v2 emits ``$defs`` inside individual request/response schemas,
    but docusaurus-plugin-openapi-docs resolves ``$ref`` against the whole
    document root, causing "Missing $ref pointer" errors.  Hoisting the
    definitions to ``components/schemas`` and rewriting ``#/$defs/Foo`` to
    ``#/components/schemas/Foo`` makes the spec compatible.
    """
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})

    def _collect(node: object) -> None:
        if isinstance(node, dict):
            if "$defs" in node:
                for name, defn in node.pop("$defs").items():
                    schemas.setdefault(name, defn)
                    _collect(defn)
            for value in node.values():
                _collect(value)
        elif isinstance(node, list):
            for item in node:
                _collect(item)

    _collect(schema)

    def _rewrite_refs(node: object) -> None:
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                if ref.startswith("#/$defs/"):
                    node["$ref"] = ref.replace("#/$defs/", "#/components/schemas/")
            for value in node.values():
                _rewrite_refs(value)
        elif isinstance(node, list):
            for item in node:
                _rewrite_refs(item)

    _rewrite_refs(schema)
    return schema


def write_openapi(output_path: Path) -> None:
    api = build_docs_api()
    schema = _hoist_defs(api.app.openapi())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(schema, indent=2) + "\n")


def write_redoc_html(output_path: Path, openapi_json_path: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    redoc_bundle_path = output_path.parent / "redoc.standalone.js"
    if not REDOC_BUNDLE_SOURCE.exists():
        raise FileNotFoundError(
            "ReDoc bundle not found. Install dashboard dependencies first so "
            f"{REDOC_BUNDLE_SOURCE} exists."
        )
    shutil.copy2(REDOC_BUNDLE_SOURCE, redoc_bundle_path)
    output_path.write_text(
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Skulk API Reference</title>
    <style>
      html,
      body {{
        margin: 0;
        padding: 0;
        height: 100%;
      }}
      #redoc-container {{
        min-height: 100vh;
      }}
      #redoc-loading {{
        font-family: system-ui, sans-serif;
        padding: 1rem;
        color: #334155;
      }}
    </style>
  </head>
  <body>
    <div id="redoc-loading">Loading API reference...</div>
    <div id="redoc-container"></div>
    <script src="./redoc.standalone.js"></script>
    <script>
      const loading = document.getElementById("redoc-loading");
      const container = document.getElementById("redoc-container");
      if (window.Redoc) {{
        window.Redoc.init("{openapi_json_path}", {{}}, container, () => {{
          if (loading) loading.remove();
        }});
      }} else if (loading) {{
        loading.textContent = "Failed to load ReDoc assets.";
      }}
    </script>
  </body>
</html>
""",
    )


def main() -> None:
    docs_root = Path("docs/generated")
    openapi_path = docs_root / "openapi.json"
    api_html_path = docs_root / "api" / "index.html"

    write_openapi(openapi_path)
    write_redoc_html(api_html_path, "../openapi.json")


if __name__ == "__main__":
    main()
