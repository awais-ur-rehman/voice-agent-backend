"""Freeze public agent configuration and dashboard request/response schemas offline."""

import argparse
import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from brain.agent_config import AgentConfig, LegacyAgentConfig
from scheduler.dashboard.agents import (
    AgentCreate,
    AgentOut,
    AgentPatch,
    DraftOut,
    DraftPut,
    Revision,
    Rollback,
    VersionOut,
    VoiceOut,
)


def render():
    _, schema = models_json_schema(
        [
            (model, "validation")
            for model in (
                AgentConfig,
                LegacyAgentConfig,
                AgentCreate,
                AgentOut,
                AgentPatch,
                DraftOut,
                DraftPut,
                Revision,
                Rollback,
                VersionOut,
                VoiceOut,
            )
        ],
        title="Dashboard agent contracts v1; agent configuration v2",
    )
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "packages/schemas/agents.v1.json"
    content = render()
    if args.check:
        if not target.is_file() or target.read_text() != content:
            raise SystemExit("Agent schema is stale; run python -m scripts.export_agent_contracts")
    else:
        target.write_text(content)
