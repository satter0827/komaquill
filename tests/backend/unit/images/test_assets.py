"""Verify images contracts within the declared test scope."""

from komaquill.application.preparation.service import requirements
from komaquill.domain.assets import workflow_models


def test_standard_requirements_exclude_muted_and_unavailable_or_nonimage_nodes():
    workflow = {
        "nodes": [
            {
                "type": "Loader",
                "widgets_values": ["model.safetensors"],
                "properties": {"models": [{"name": "model.safetensors"}]},
            },
            {"type": "Loader", "mode": 4, "widgets_values": ["ignored.sft"]},
            {"type": "Missing", "widgets_values": ["unknown.sft"]},
            {"type": "LoadAudio"},
            {"type": "Custom"},
        ]
    }
    hints, unsupported = requirements(
        workflow,
        {
            "Loader": {"input": {}},
            "LoadAudio": {"python_module": "nodes"},
            "Custom": {"python_module": "custom"},
        },
    )
    assert hints == [{"name": "model.safetensors", "node_id": ""}]
    assert set(unsupported) == {"Missing", "unknown.sft", "LoadAudio", "Custom"}
    assert workflow_models({}) == []
