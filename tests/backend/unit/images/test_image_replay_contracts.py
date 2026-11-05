"""Verify images contracts within the declared test scope."""

import pytest
from komaquill.adapters.configuration import Settings
from komaquill.adapters.workflows import prepare
from komaquill.domain.configuration import RuntimeSettings
from komaquill.domain.errors import KomaquillError
from komaquill.domain.images import compile_image_input
from komaquill.domain.presets import GenerationPreset


@pytest.mark.parametrize("name", ["klein-4b", "klein-4b-edit"])
def test_preset_fingerprint_survives_default_expansion_and_json_replay(name):
    settings = Settings(models=[name.removesuffix("-edit")])
    profile = GenerationPreset.model_validate(
        settings.presets[name].model_dump(mode="json", exclude_defaults=True)
    )
    settings = settings.model_copy(update={"presets": {name: profile}})
    compiled = compile_image_input(profile, "rainy street", subjects=("human", "cat"))
    retained = RuntimeSettings.model_validate_json(settings.model_dump_json())
    graph, ui = prepare(
        "ignored", 42, "test", settings=retained, compiled_input=compiled, profile_name=name
    )
    expected = prepare(
        "rainy street",
        42,
        "test",
        settings=settings,
        subject_prompts=("human", "cat"),
        profile_name=name,
    )
    assert (graph, ui) == expected


def old_compilation(profile):
    from komaquill.domain.prompts import joined

    value = compile_image_input(profile, "a cat").model_dump()
    value.update(
        format_version=3,
        compiler_version=6,
        preset_sha256="a" * 64,
        requested_negative=None,
        negative_prefix="",
        negative="",
        negative_body="",
        subject_positives=tuple(
            joined(profile.positive_prefix, text) for text in value["subject_prompts"]
        ),
        layout="global",
    )
    return value


def test_retained_old_compilation_is_readable_but_cannot_be_executed():
    from komaquill.domain.retained_images import RetainedImageInput

    settings = Settings()
    retained = RetainedImageInput.model_validate(old_compilation(settings.presets["klein-4b"]))
    original = retained.model_dump_json()
    assert RetainedImageInput.model_validate_json(original).model_dump_json() == original
    with pytest.raises(KomaquillError):
        prepare("unused", 42, "test", settings=settings, compiled_input=retained)


def test_self_consistent_but_wrong_quality_conditions_are_rejected():
    from komaquill.domain.images import CompiledImageInput

    settings = Settings(models=["klein-4b"])
    compiled = compile_image_input(settings.presets["klein-4b"], "street", subjects=("cat",))
    data = compiled.model_dump()
    data.update(quality_enabled=False, positive_prefix="", positive=data["body"])
    changed = CompiledImageInput.model_validate(data)
    with pytest.raises(KomaquillError) as error:
        prepare("unused", 42, "test", settings=settings, compiled_input=changed)
    assert error.value.code == "invalid_configuration"


def test_legacy_blank_subject_metadata_remains_readable():
    from komaquill.domain.retained_images import RetainedImageInput

    profile = Settings(models=["klein-4b"]).presets["klein-4b"]
    value = old_compilation(profile)
    value.update(
        compiler_version=1, subject_prompts=(" ",), subject_positives=(profile.positive_prefix,)
    )
    retained = RetainedImageInput.model_validate(value)
    assert retained.compiler_version == 1 and retained.subject_prompts == (" ",)
