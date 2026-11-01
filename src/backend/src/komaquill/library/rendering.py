"""Resolve visible preset fields before translation and change detection."""

from dataclasses import dataclass

from komaquill.library.models import Preset, PresetInput, PresetKind, PresetSaveOptions


@dataclass(frozen=True)
class PresetChanges:
    content_changed: bool
    image_changed: bool
    generate_image: bool


def preset_changes(
    kind: PresetKind, value: PresetInput, previous: Preset | None, options: PresetSaveOptions
) -> PresetChanges:
    content = (
        previous is None
        or previous.model_dump(include=set(type(value).model_fields)) != value.model_dump()
    )
    image = previous is None or preset_image_input(kind, previous) != preset_image_input(
        kind, value
    )
    generate = options.image_policy == "always" or (options.image_policy == "auto" and image)
    return PresetChanges(content, image, generate)


def preset_image_input(kind: PresetKind, value: Preset | PresetInput) -> dict[str, object]:
    fields = (
        ("age", "gender", "appearance", "role", "description")
        if kind == "character"
        else ("world", "place", "description")
    )
    data = value.model_dump(mode="json")
    result = {key: data[key] for key in fields if data.get(key)}
    if kind == "story" and not result:
        result["opening"] = data.get("opening", "")
    return result
