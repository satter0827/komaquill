"""Keep saved settings and creation operations independent of infrastructure."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field

from komaquill.domain.conversations.models import now
from komaquill.domain.errors import OperationStage
from komaquill.domain.images import MAX_BROWSER_SEED, ImageGenerationConditions, ImageSeed
from komaquill.domain.roleplay import CharacterDefinition, PersonDefinition, Record, StoryDefinition

Locale = Literal["ja", "en"]
PresetKind = Literal["player", "character", "story"]


class PlayerInput(PersonDefinition):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")


class CharacterInput(CharacterDefinition):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")


class StoryInput(StoryDefinition):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    character_id: UUID | None = None
    tone: str = Field(default="", max_length=2000)
    topics: str = Field(default="", max_length=3000)
    avoid: str = Field(default="", max_length=3000)


class SavedPreset(Record):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )
    id: UUID = Field(default_factory=uuid4)
    revision: int = Field(default=0, ge=0)
    image_id: UUID | None = None
    image_job_id: UUID | None = None
    image_stale: bool = False
    sample: bool = False
    sample_locale: Locale | None = None


class PlayerProfile(PlayerInput, SavedPreset):
    pass


class CharacterPreset(CharacterInput, SavedPreset):
    pass


class StoryPreset(StoryInput, SavedPreset):
    pass


Preset = PlayerProfile | CharacterPreset | StoryPreset
PresetInput = PlayerInput | CharacterInput | StoryInput


MAX_IMAGE_PROMPT_LENGTH = 12000


class WaiPreferences(Record):
    quality_enabled: bool | None = None
    quality_positive: str | None = Field(default=None, max_length=MAX_IMAGE_PROMPT_LENGTH)
    quality_negative: str | None = Field(default=None, max_length=MAX_IMAGE_PROMPT_LENGTH)
    candidates: int | None = Field(default=None, ge=1, strict=True)
    seed: ImageSeed | None = Field(default=None, le=MAX_BROWSER_SEED)
    width: int | None = Field(default=None, strict=True)
    height: int | None = Field(default=None, strict=True)
    steps: int | None = Field(default=None, strict=True)
    cfg: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    sampler: str | None = Field(default=None, min_length=1)
    scheduler: str | None = Field(default=None, min_length=1)
    detail_strength: float | None = Field(default=None, strict=True, allow_inf_nan=False)


class ImagePromptDefaults(Record):
    positive: str = Field(default="", max_length=MAX_IMAGE_PROMPT_LENGTH)
    negative: str = Field(default="", max_length=MAX_IMAGE_PROMPT_LENGTH)
    wai: WaiPreferences = Field(default_factory=WaiPreferences)


class Preferences(Record):
    revision: int = Field(default=0, ge=0)
    locale: Locale = "ja"
    font_size: Literal["normal", "large"] = "normal"
    density: Literal["comfortable", "compact"] = "comfortable"
    response_length: Literal["short", "standard", "detailed"] = "standard"
    llm_profile: str = "lmstudio-4b"
    image_prompts: ImagePromptDefaults = Field(default_factory=ImagePromptDefaults)


class PresetRef(Record):
    id: UUID
    revision: int = Field(ge=0)


class ConversationStart(Record):
    player_revision: int = Field(ge=0)
    character: PresetRef
    story: PresetRef
    title: str = Field(default="", max_length=120)
    locale: Locale = "ja"
    llm_profile: str | None = None


class CreationRequest(Record):
    subjects: Literal["character", "pair", "environment"] = "character"
    viewpoint: Literal["player", "observer"] = "player"
    operation_id: UUID
    kind: Literal["character_draft", "story_draft", "portrait", "banner", "conversation_image"]
    target_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    prompt: str = Field(default="", max_length=MAX_IMAGE_PROMPT_LENGTH)
    locale: Locale = "ja"
    llm_profile: str = "lmstudio-4b"
    allow_real_llm: bool = False


class CreationJob(Record):
    nsfw_enabled: bool = False
    id: UUID
    request: CreationRequest
    state: Literal["accepted", "running", "submitted", "complete", "failed", "interrupted"]
    original: str = ""
    image_prompts: ImagePromptDefaults = Field(default_factory=ImagePromptDefaults)
    prompt_en: str | None = None
    image_generation: ImageGenerationConditions | None = Field(default=None, exclude=True)
    result: CharacterInput | StoryInput | None = None
    image_id: UUID | None = None
    turn_id: UUID | None = None
    error: str | None = None
    error_stage: OperationStage | None = None
    created_at: str = Field(default_factory=lambda: now().isoformat())


class PreparedImage(Record):
    text: str = Field(min_length=1, max_length=20000)


class ConversationImageRequest(Record):
    subjects: Literal["character", "pair", "environment"] = "character"
    viewpoint: Literal["player", "observer"] = "player"
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    locale: Locale = "ja"


class PresetSaveOptions(Record):
    operation_id: UUID
    target_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class CharacterSaveRequest(PresetSaveOptions):
    value: CharacterInput


class StorySaveRequest(PresetSaveOptions):
    value: StoryInput


class PresetSaveResult(Record):
    preset: CharacterPreset | StoryPreset
