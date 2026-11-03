"""Keep saved settings and creation operations independent of infrastructure."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field

from komaquill.conversations.models import now
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


class SavedPreset(Record):
    model_config = ConfigDict(
        extra="forbid", frozen=True, json_schema_serialization_defaults_required=True
    )
    id: UUID = Field(default_factory=uuid4)
    revision: int = Field(default=0, ge=0)
    image_id: UUID | None = None
    image_job_id: UUID | None = None
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


class Preferences(Record):
    nsfw_enabled: bool = False
    revision: int = Field(default=0, ge=0)
    locale: Locale = "ja"
    font_size: Literal["normal", "large"] = "normal"
    density: Literal["comfortable", "compact"] = "comfortable"
    response_length: Literal["short", "standard", "detailed"] = "standard"
    llm_profile: str = "lmstudio-4b"


class PresetRef(Record):
    id: UUID
    revision: int = Field(ge=0)


class ConversationStart(Record):
    tone: str = Field(default="", max_length=2000)
    topics: str = Field(default="", max_length=3000)
    avoid: str = Field(default="", max_length=3000)
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
    prompt: str = Field(default="", max_length=12000)
    locale: Locale = "ja"
    llm_profile: str = "lmstudio-4b"
    allow_real_llm: bool = False


class CreationJob(Record):
    nsfw_enabled: bool = False
    id: UUID
    request: CreationRequest
    state: Literal["accepted", "running", "submitted", "complete", "failed", "interrupted"]
    original: str = ""
    prompt_en: str | None = None
    result: CharacterInput | StoryInput | None = None
    image_id: UUID | None = None
    turn_id: UUID | None = None
    error: str | None = None
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
    image_policy: Literal["auto", "always", "none"] = "auto"
    locale: Locale = "ja"
    llm_profile: str = "lmstudio-4b"
    allow_real_llm: bool = False


class CharacterSaveRequest(PresetSaveOptions):
    value: CharacterInput


class StorySaveRequest(PresetSaveOptions):
    value: StoryInput


class PresetSaveResult(Record):
    preset: CharacterPreset | StoryPreset
    job: CreationJob | None = None
    image_error: str | None = None
