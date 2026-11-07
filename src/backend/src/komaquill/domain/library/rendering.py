"""Resolve visible preset fields before translation and change detection."""

from dataclasses import dataclass

from komaquill.domain.library.models import Preset, PresetInput, PresetKind
from komaquill.domain.roleplay import PersonDefinition, visual_appearance


@dataclass(frozen=True)
class PresetChanges:
    content_changed: bool
    image_changed: bool


def preserve_appearance_reference(value: PresetInput, previous: Preset | None) -> PresetInput:
    """Keep the retained marker while an unrelated preset field is edited."""
    if isinstance(value, PersonDefinition) and isinstance(previous, PersonDefinition):
        return value.model_copy(
            update={
                "visual_review_required": previous.visual_review_required
                and value.appearance == previous.appearance
            }
        )
    return value


def preset_changes(kind: PresetKind, value: PresetInput, previous: Preset | None) -> PresetChanges:
    content = (
        previous is None
        or previous.model_dump(include=set(type(value).model_fields)) != value.model_dump()
    )
    image = previous is None or preset_image_input(kind, previous) != preset_image_input(
        kind, value
    )
    return PresetChanges(content, image)


def preset_image_input(kind: PresetKind, value: Preset | PresetInput) -> dict[str, object]:
    fields = (
        ("age", "gender", "initial_clothing", "initial_held_objects")
        if kind == "character"
        else ("world", "place", "description")
    )
    data = value.model_dump(mode="json")
    result = {
        key: data[key]
        for key in fields
        if data.get(key)
        or (
            kind == "character"
            and key in {"initial_clothing", "initial_held_objects"}
            and data.get(key) is not None
        )
    }
    if kind == "story" and not result:
        result["opening"] = data.get("opening", "")
    if kind == "character" and isinstance(value, PersonDefinition):
        result.update(
            (key, text)
            for key, text in visual_appearance(
                value, value.initial_clothing, field="appearance"
            ).items()
            if text
        )
    return result
