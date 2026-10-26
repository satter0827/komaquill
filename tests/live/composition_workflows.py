"""Build the fixed A/B comparison bundles without changing the packaged presets."""

import json
from copy import deepcopy
from pathlib import Path

from komaquill.infrastructure.configuration import Settings


def prepare_comparison(workspace: Path) -> None:
    """Keep the experiment graphs frozen after their first preparation."""
    source = Settings().presets["story-inpaint"]
    retained = {3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 30, 31, 35, 36, 40}
    graph = {key: deepcopy(node) for key, node in source.api.items() if int(key) in retained}
    graph["3"]["inputs"].update(model=["4", 0], latent_image=["37", 0], denoise=0.35)
    graph["33"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["40", 0], "vae": ["4", 2]}}
    graph["37"] = {
        "class_type": "SetLatentNoiseMask",
        "inputs": {"samples": ["33", 0], "mask": ["31", 0]},
    }
    graph["50"] = {"class_type": "LoadImage", "inputs": {"image": "foreground.png"}}
    graph["51"] = {
        "class_type": "LoadImageMask",
        "inputs": {"image": "foreground-mask.png", "channel": "red"},
    }
    graph["66"] = {
        "class_type": "ImageCompositeMasked",
        "inputs": {
            "destination": ["30", 0],
            "source": ["50", 0],
            "mask": ["51", 0],
            "x": 0,
            "y": 0,
            "resize_source": False,
        },
    }
    graph["40"]["inputs"]["image"] = ["66", 0]
    graph["35"]["inputs"]["source"] = ["8", 0]
    graph["36"]["inputs"]["images"] = ["66", 0]
    templates = {row["id"]: row for row in source.ui["nodes"]}
    nodes = {key: deepcopy(templates[int(key)]) for key in graph if int(key) in templates}
    nodes["33"] = deepcopy(templates[33])
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
    nodes["50"] = deepcopy(templates[30])
    nodes["50"]["id"] = 50
    nodes["51"] = deepcopy(templates[31])
    nodes["51"]["id"] = 51
    nodes["66"] = deepcopy(templates[35])
    nodes["66"]["id"] = 66
    for key, node in nodes.items():
        node["properties"] = {"Node name for S&R": node["type"]}
        for slot in node["inputs"]:
            slot["link"] = None
        for slot in node["outputs"]:
            slot["links"] = []
        node["pos"] = [(int(key) % 5) * 420, (int(key) // 5) * 230]
    permitted = {
        "seed",
        "steps",
        "cfg",
        "sampler",
        "scheduler",
        "checkpoint",
        "negative",
        "output_prefix",
        "positive_prefix",
        "prompt",
        "clip_skip",
        "prompt_delimiter",
        "negative_prefix",
        "negative_delimiter",
        "input_base",
        "input_mask",
        "batch_size",
        "denoise",
    }
    bindings = [row.model_dump() for row in source.bindings if row.source in permitted]
    for name, node in [("foreground", "50"), ("foreground_mask", "51")]:
        bindings.append(
            dict(
                source=f"input_{name}",
                api_node=node,
                api_input="image",
                ui_node=int(node),
                ui_widget=0,
            )
        )
    for name, final in [("composition-only", ["40", 0]), ("composition-refine", ["35", 0])]:
        folder = workspace / "presets" / name
        if folder.exists():
            continue
        api = deepcopy(graph)
        ui_nodes = deepcopy(nodes)
        api["9"]["inputs"]["images"] = final
        links = []
        for key, node in api.items():
            for input_name, value in node["inputs"].items():
                if not isinstance(value, list):
                    continue
                target = ui_nodes[key]
                target_slot = next(
                    i for i, slot in enumerate(target["inputs"]) if slot["name"] == input_name
                )
                source_slot = ui_nodes[value[0]]["outputs"][value[1]]
                identity = len(links) + 1
                links.append(
                    [identity, int(value[0]), value[1], int(key), target_slot, source_slot["type"]]
                )
                target["inputs"][target_slot]["link"] = identity
                source_slot["links"].append(identity)
        ui = {
            "last_node_id": 66,
            "last_link_id": len(links),
            "nodes": list(ui_nodes.values()),
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
                        ["50", "image"],
                        ["51", "image"],
                        ["31", "image"],
                        ["40", "amount"],
                    ],
                    "outputs": ["9"],
                },
            },
        }
        text = (
            "\n".join(
                f"{key} = {json.dumps(value)}"
                for key, value in {
                    "batch_size": 2,
                    "primary_output_node": "9",
                    "checkpoint": "pony",
                    "width": 1024,
                    "height": 1024,
                    "steps": 25,
                    "cfg": 7.0,
                    "sampler": "euler_ancestral",
                    "scheduler": "normal",
                    "clip_skip": -2,
                    "positive_prefix": source.positive_prefix,
                    "negative_prefix": source.negative_prefix,
                    "negative": "",
                    "sample_prompt": "gray mother and kitten",
                }.items()
            )
            + "\n[parameters]\ndenoise = 0.35\n[parameter_bounds]\ndenoise = [0.0, 1.0]"
            + '\n[output_stages]\n"36" = "composition"\n'
        )
        for slot, kind in [
            ("base", "image"),
            ("foreground", "image"),
            ("foreground_mask", "mask"),
            ("mask", "mask"),
        ]:
            text += f'\n[image_inputs.{slot}]\nkind = "{kind}"\nsample = "{slot}.png"\n'
            if slot != "base":
                text += "match_base = true\n"
        for binding in bindings:
            text += (
                "\n[[bindings]]\n"
                + "\n".join(f"{key} = {json.dumps(value)}" for key, value in binding.items())
                + "\n"
            )
        folder.mkdir(parents=True)
        (folder / "preset.toml").write_text(text, encoding="utf-8")
        (folder / "workflow-api.json").write_text(json.dumps(api, indent=2), encoding="utf-8")
        (folder / "workflow.json").write_text(json.dumps(ui, indent=2), encoding="utf-8")
