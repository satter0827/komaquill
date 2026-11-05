"""Inspect image workflow requirements without selecting a GPU backend."""

from typing import Any

from komaquill.domain.assets import workflow_models


def requirements(
    workflow: dict[str, Any], definitions: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Reject missing or nonstandard nodes and identify unresolved model references."""
    unsupported: list[str] = []
    hints = workflow_models(workflow)
    names = {item.get("name") for item in hints}
    for node in workflow.get("nodes", []):
        if not isinstance(node, dict) or node.get("mode", 0) in {2, 4}:
            continue
        kind = node.get("type", "")
        definition = definitions.get(kind)
        module = definition.get("python_module", "nodes") if isinstance(definition, dict) else ""
        if (
            not definition
            or not (module == "nodes" or module.startswith("comfy_extras."))
            or any(part in kind.lower() for part in ("video", "audio"))
        ):
            unsupported.append(str(kind))
        for value in node.get("widgets_values", []):
            if (
                isinstance(value, str)
                and value.lower().endswith((".safetensors", ".sft"))
                and value not in names
            ):
                # Installed choices are usable; absent files need source metadata.
                inputs = definition.get("input", {}) if definition else {}
                available = {
                    v
                    for group in inputs.values()
                    if isinstance(group, dict)
                    for spec in group.values()
                    if isinstance(spec, list) and spec
                    for v in (
                        spec[0]
                        if isinstance(spec[0], list)
                        else spec[1].get("options", [])
                        if len(spec) > 1 and isinstance(spec[1], dict)
                        else []
                    )
                    if isinstance(v, str)
                }
                if value not in available:
                    unsupported.append(value)
    return hints, sorted(set(unsupported))
