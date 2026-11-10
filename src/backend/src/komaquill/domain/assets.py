"""Define workflow preparation requests and content-addressed model records."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from komaquill.domain.errors import FailureInfo
from komaquill.domain.presets import ModelPreset

PROTOCOL_VERSION = 2
PREPARATION_TERMINAL = frozenset({"ready", "failed", "cancelled"})


class Preparation(BaseModel):
    """Keep confirmation and local preservation distinct from remote placement."""

    model_config = ConfigDict(extra="forbid")
    format_version: Literal[2] = 2
    request_id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: Literal[
        "requested", "confirmation", "confirmed", "preparing", "ready", "failed", "cancelled"
    ] = "requested"
    workflow: dict[str, Any]
    assets: list[ModelPreset] = Field(default_factory=list)
    node_models: dict[str, dict[str, str]] = Field(default_factory=dict)
    unsupported: list[str] = Field(default_factory=list)
    reused: list[str] = Field(default_factory=list)
    current_asset: str | None = None
    phase_started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    remote_ready: bool = False
    failure: FailureInfo | None = None


def active_workflow_nodes(workflow: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield executable node instances, excluding bypassed subgraphs."""
    metadata = workflow.get("definitions")
    subgraphs = metadata.get("subgraphs", []) if isinstance(metadata, dict) else []
    definitions = {
        definition["id"]: definition
        for definition in (subgraphs if isinstance(subgraphs, list) else [])
        if isinstance(definition, dict) and isinstance(definition.get("id"), str)
    }
    pending: list[tuple[str, dict[str, Any], frozenset[str]]] = [("", workflow, frozenset())]
    while pending:
        prefix, graph, parents = pending.pop()
        nodes = graph.get("nodes")
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict) or node.get("mode", 0) in {2, 4}:
                continue
            identity = f"{prefix}{node.get('id', '')}"
            kind = node.get("type")
            if isinstance(kind, str) and kind in definitions and kind not in parents:
                pending.append((f"{identity}:", definitions[kind], parents | {kind}))
            else:
                yield identity, node


def workflow_models(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Read standard template model hints from active graph nodes."""
    result = []
    for identity, node in active_workflow_nodes(workflow):
        values = node.get("widgets_values", [])
        for model in node.get("properties", {}).get("models", []):
            if isinstance(model, dict) and model.get("name") in values:
                result.append({**model, "node_id": identity})
    return result


def workflow_image_slots(workflow: dict[str, Any]) -> Iterator[tuple[list[Any], str]]:
    """Find active standard image widgets and their input references."""
    for _, node in active_workflow_nodes(workflow):
        if node.get("type") not in {"LoadImage", "LoadImageMask"}:
            continue
        values = node.get("widgets_values")
        if isinstance(values, list) and values and isinstance(values[0], str):
            yield values, values[0].removesuffix(" [input]")
