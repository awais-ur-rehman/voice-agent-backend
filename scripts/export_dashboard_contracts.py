"""Export public staff contracts without creating the app or loading settings."""

import argparse
import json
from pathlib import Path

from fastapi import FastAPI
from pydantic.json_schema import models_json_schema

from brain import draft_test_routes, voice_routes
from brain.agent_config import AgentConfig, LegacyAgentConfig
from scheduler import appointments, auth, catalog
from scheduler.dashboard import agents, patients, profile, scheduling

ROOT = Path(__file__).resolve().parents[1]


def render() -> str:
    app = FastAPI(title="Dashboard API", version="1.0.0")
    # Explicit staff-only router allowlist. Never mount runtime or worker routers.
    for router in (
        auth.router,
        catalog.router,
        appointments.router,
        agents.router,
        patients.router,
        profile.router,
        scheduling.router,
        draft_test_routes.router,
        voice_routes.router,
    ):
        app.include_router(router)
    schema = app.openapi()
    _, config = models_json_schema(
        [(AgentConfig, "validation"), (LegacyAgentConfig, "validation")],
        ref_template="#/components/schemas/{model}",
    )
    schema["components"]["schemas"].update(config["$defs"])
    for name in ("DraftOut", "VersionOut"):
        schema["components"]["schemas"][name]["properties"]["config"] = {
            "anyOf": [
                {"$ref": "#/components/schemas/AgentConfig"},
                {"$ref": "#/components/schemas/LegacyAgentConfig"},
            ]
        }
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "packages/schemas/dashboard.v1.json"
    content = render()
    if args.check:
        if not target.exists() or target.read_text() != content:
            raise SystemExit(
                "Dashboard schema is stale; run python -m scripts.export_dashboard_contracts"
            )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
