"""Derive a bounded local repair preset from the pinned inpaint workflow."""

import json
from copy import deepcopy
from pathlib import Path

from komaquill.infrastructure.configuration import Settings

PROFILE = "completion-repair"


def prepare_repair(workspace: Path) -> None:
    """Keep only the base canvas, edit mask and standard sampling path."""
    source = Settings().presets["story-inpaint"]
    defaults = Settings().presets["pony-story"]
    retained = ("3", "4", "6", "7", "8", "9", "10", "11", "12", "13", "30", "31", "35", "40")
    api = {key: deepcopy(node) for key, node in source.api.items() if key in retained}
    api["3"]["inputs"].update(model=["4", 0], latent_image=["37", 0], denoise=0.55)
    api["33"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["40", 0], "vae": ["4", 2]}}
    api["37"] = {
        "class_type": "SetLatentNoiseMask",
        "inputs": {"samples": ["33", 0], "mask": ["31", 0]},
    }
    api["35"]["inputs"]["source"] = ["8", 0]
    templates = {str(node["id"]): node for node in source.ui["nodes"]}
    nodes = {key: deepcopy(templates[key]) for key in retained}
    nodes["33"] = deepcopy(templates["33"])
    nodes["33"].update(type="VAEEncode", widgets_values=[])
    nodes["33"]["inputs"] = [
        {"name": "pixels", "type": "IMAGE", "link": None},
        {"name": "vae", "type": "VAE", "link": None},
    ]
    nodes["37"] = deepcopy(nodes["33"])
    nodes["37"].update(id=37, type="SetLatentNoiseMask")
    nodes["37"]["inputs"] = [
        {"name": "samples", "type": "LATENT", "link": None},
        {"name": "mask", "type": "MASK", "link": None},
    ]
    for key, node in nodes.items():
        node.pop("title", None)
        node["properties"] = {"Node name for S&R": node["type"]}
        node["pos"] = [(int(key) % 5) * 420, (int(key) // 5) * 230]
        for slot in node["inputs"]:
            slot["link"] = None
        for slot in node["outputs"]:
            slot["links"] = []
    links = []
    for key, node in api.items():
        for name, value in node["inputs"].items():
            if not isinstance(value, list):
                continue
            index = next(i for i, slot in enumerate(nodes[key]["inputs"]) if slot["name"] == name)
            origin = nodes[value[0]]["outputs"][value[1]]
            identity = len(links) + 1
            links.append([identity, int(value[0]), value[1], int(key), index, origin["type"]])
            nodes[key]["inputs"][index]["link"] = identity
            origin["links"].append(identity)
    ui = {
        "last_node_id": 40,
        "last_link_id": len(links),
        "nodes": list(nodes.values()),
        "links": links,
        "groups": [],
        "config": {},
        "version": 0.4,
        "extra": {
            "linearMode": False,
            "linearData": {
                "inputs": [
                    ["10", "string_b"],
                    ["12", "string_b"],
                    ["30", "image"],
                    ["31", "image"],
                    ["40", "amount"],
                ],
                "outputs": ["9"],
            },
        },
    }
    names = {
        "checkpoint",
        "width",
        "height",
        "steps",
        "cfg",
        "sampler",
        "scheduler",
        "clip_skip",
        "quality_enabled",
        "positive_prefix",
        "negative_prefix",
        "negative",
        "batch_size",
    }
    values = defaults.model_dump(mode="json", include=names)
    values.update(primary_output_node="9", sample_prompt="local correction of the existing scene")
    text = "\n".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
    text += "\n[parameters]\ndenoise = 0.55\n[parameter_bounds]\ndenoise = [0.0, 1.0]\n"
    text += '\n[image_inputs.base]\nkind = "image"\nsample = "base.png"\n'
    text += '\n[image_inputs.mask]\nkind = "mask"\nsample = "mask.png"\nmatch_base = true\n'
    for binding in source.bindings:
        if binding.api_node not in api:
            continue
        text += (
            "\n[[bindings]]\n"
            + "\n".join(
                f"{key} = {json.dumps(value)}" for key, value in binding.model_dump().items()
            )
            + "\n"
        )
    folder = workspace / "presets" / PROFILE
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "preset.toml").write_text(text, encoding="utf-8")
    (folder / "workflow-api.json").write_text(json.dumps(api, indent=2), encoding="utf-8")
    (folder / "workflow.json").write_text(json.dumps(ui, indent=2), encoding="utf-8")
