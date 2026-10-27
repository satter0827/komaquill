"""Define portable story plans and image-render attempts."""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from komaquill.domain.errors import FailureInfo
from komaquill.domain.images import CompiledInputRecord, PromptSourceRecord
from komaquill.domain.workflow import digest


class StoryRecord(BaseModel):
    """Reject unknown fields and ambiguous timestamps in durable records."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*")
    @classmethod
    def nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Text fields must not be blank.")
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Timestamps require a timezone.")
        return value


class Character(StoryRecord):
    kind: Literal["human", "animal", "other"] | None = None
    key: str
    name: str
    appearance: str = Field(
        description=(
            "English only, at most 15 words: stable anatomy, hair/fur and clothing. "
            "Exclude actions, possessions, location and temporary states such as wetness."
        )
    )


ActorKey = Annotated[
    str,
    Field(
        pattern=r"^[a-z]+(?:_[a-z]+)*$",
        description="Short descriptive English identity, not a numbered placeholder.",
    ),
]
PropKey = Annotated[
    str,
    Field(
        pattern=r"^[a-z]+(?:_[a-z]+)*$",
        description="Short descriptive English identity of this object.",
    ),
]


class PlannedCharacter(Character):
    """Require an explicit subject category in newly planned stories."""

    key: ActorKey
    kind: Literal["human", "animal", "other"] = Field(...)


class StoryProp(StoryRecord):
    key: str
    name: str
    appearance: str = Field(description="English: stable visible properties, such as color.")


class SceneSubject(StoryRecord):
    character: str = Field(description="Character key; list visible subjects from left to right.")
    action: str = Field(description="English: one visible pose or action, without appearance.")


class ScenePropState(StoryRecord):
    prop: str
    holder: str | None = Field(description="Visible holder's character key, or null if unheld.")
    visible: bool = True
    state: str = Field(description="English: visible state and position at this instant.")


class PlannedProp(StoryProp):
    key: PropKey


class PlannedSubject(SceneSubject):
    character: ActorKey


class PlannedPropState(ScenePropState):
    prop: PropKey
    holder: ActorKey | None
    visible: bool = Field(
        description=(
            "Whether this object is actually visible in this instant. False inside an opaque "
            "closed container or pocket. Do not invent an unseen object's location."
        )
    )


class SceneText(StoryRecord):
    title: str
    narrative: str = Field(
        description=(
            "Japanese story prose. Together all scenes tell the whole source through its ending. "
            "Explicitly preserve material actions; paragraph references alone are not coverage. "
            "For one scene, summarize the complete story."
        )
    )
    visual_description: str = Field(
        description=(
            "English only, at most 20 words: framing and environment of one instant. "
            "Do not repeat characters, appearances or actions here."
        )
    )
    location: str | None
    source_refs: list[str] = Field(
        description=(
            "Paragraph IDs narrated by this scene. Across all scenes cover every source paragraph. "
            "For one scene list ALL IDs."
        )
    )


class SceneDraft(SceneText):
    subjects: list[PlannedSubject]
    props: list[PlannedPropState]


class StoryDraft(StoryRecord):
    """Keep model output small and independent of image-engine settings."""

    title: str
    synopsis: str
    characters: list[PlannedCharacter] = Field(
        description=(
            "Characters actually present in the source. Empty for a story containing only objects. "
            "Do not invent a person to fill this list; moving objects belong to props."
        )
    )
    props: list[PlannedProp]
    scenes: list[SceneDraft]
    supplements: list[str] = Field(
        description=(
            "Japanese: inferred visual details absent from the source, not compliance claims."
        )
    )


class StoryInput(StoryRecord):
    format_version: Literal[1] = 1
    story_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_name: str
    source_text: str
    source_sha256: str
    image_count: int | Literal["auto"] = "auto"
    max_images: int | None = Field(default=None, ge=1)
    purpose: Literal["work", "verification"] = "work"
    case_id: str | None = None

    @property
    def paragraphs(self) -> dict[str, str]:
        parts = [
            text
            for text in re.split(r"\n\s*\n", self.source_text.replace("\r\n", "\n"))
            if text.strip()
        ]
        return {f"p{index}": text for index, text in enumerate(parts, 1)}

    @model_validator(mode="after")
    def valid_source(self) -> Self:
        if isinstance(self.image_count, int) and self.image_count < 1:
            raise ValueError("The image count must be positive.")
        if self.image_count == "auto" and self.max_images is None:
            raise ValueError("Automatic image selection requires an upper bound.")
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("The source checksum does not match.")
        return self


class Scene(SceneText):
    """Read retained scenes without inventing missing visual specifications."""

    subjects: list[SceneSubject] | None = None
    props: list[ScenePropState] = Field(default_factory=list)
    characters: list[str]
    scene_id: UUID
    position: int = Field(ge=1)


class TokenUsage(StoryRecord):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class PlanningResponse(BaseModel):
    """Keep visible output separate from credentials and wire responses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    refusal: bool = False
    content: str
    model: str | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    system_fingerprint: str | None = None


class PlanningIssue(StoryRecord):
    code: str
    path: tuple[str | int, ...] = ()


class PlanningValidationError(ValueError):
    """Carry stable acceptance categories without quoting model output."""

    def __init__(
        self, code: str, explanation: str, *, issues: list[PlanningIssue] | None = None
    ) -> None:
        super().__init__(explanation)
        self.issue = PlanningIssue(code=code)
        self.issues = issues if issues is not None else [self.issue]


class PlanningOutcome(StoryRecord):
    format_version: Literal[1] = 1
    status: Literal["succeeded", "rejected", "failed", "interrupted"]
    started_at: datetime
    finished_at: datetime
    attempts: int = Field(ge=0)
    repair_limit: int = Field(ge=0, le=1)
    failure: FailureInfo | None = None


class PlanningAttempt(StoryRecord):
    format_version: Literal[1] = 1
    attempt: int = Field(ge=1)
    started_at: datetime
    finished_at: datetime
    response: PlanningResponse | None = None
    failure: FailureInfo | None = None
    correction: str | None = None
    output_schema: dict[str, Any] = Field(default_factory=dict)
    validation_issues: list[PlanningIssue] | None = None


class PlannerProvenance(StoryRecord):
    profile: str
    provider: str
    configured_model: str
    instructions: str
    instruction_sha256: str
    timeout_seconds: float
    max_output_tokens: int
    repair_attempts: int | None = Field(default=None, ge=0, le=1)
    require_model_match: bool = False
    base_url: str | None = None
    reasoning_effort: str | None = None
    temperature: float | None = None
    app_identity: dict[str, Any] = Field(default_factory=dict)
    image_profile: str | None = None
    planning_policy: dict[str, Any] = Field(default_factory=dict)


class StoryPlan(StoryRecord):
    format_version: Literal[1, 2] = 1
    request: StoryInput
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    title: str
    synopsis: str
    characters: list[Character]
    props: list[StoryProp] = Field(default_factory=list)
    scenes: list[Scene]
    supplements: list[str]
    planner: PlannerProvenance

    @model_validator(mode="after")
    def references(self) -> Self:
        validate_structure(self, self.request)
        if self.format_version == 2:
            if any(character.kind is None for character in self.characters):
                raise ValueError("New plans require explicit subject categories.")
            validate_visual_references(self)
        if [scene.position for scene in self.scenes] != list(range(1, len(self.scenes) + 1)):
            raise ValueError("Scene positions must be consecutive.")
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Scene identifiers must be distinct.")
        return self


class SceneAttempts(StoryRecord):
    compiled_input: CompiledInputRecord | None = None
    image_profile: str | None = None
    preset: dict[str, Any] | None = None
    scene_id: UUID
    seed: int = Field(ge=0, le=2**64 - 1)
    run_ids: list[UUID] = Field(default_factory=list)


class StoryRender(StoryRecord):
    """Freeze image settings while retaining independent request attempts."""

    input_sources: tuple[PromptSourceRecord, ...] = ()
    format_version: Literal[1, 2, 3, 4, 5, 6] = 1
    render_id: UUID = Field(default_factory=uuid4)
    story_id: UUID
    plan_sha256: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    image_profile: str
    settings: dict[str, Any]
    scenes: list[SceneAttempts]

    @model_validator(mode="after")
    def distinct_requests(self) -> Self:
        if self.format_version >= 2 and any(scene.compiled_input is None for scene in self.scenes):
            raise ValueError("New rendering attempts require frozen image inputs.")
        if self.format_version >= 4 and any(
            scene.preset is None
            or scene.image_profile not in self.settings.get("profiles", [])
            or scene.compiled_input is None
            or (
                self.format_version >= 6
                and scene.compiled_input.preset_sha256 != digest(scene.preset)
            )
            for scene in self.scenes
        ):
            raise ValueError("Each scene requires its frozen profile and matching image input.")
        identities = [value for scene in self.scenes for value in scene.run_ids]
        if len(identities) != len(set(identities)):
            raise ValueError("Image request identifiers must be distinct.")
        if len({scene.scene_id for scene in self.scenes}) != len(self.scenes):
            raise ValueError("Scene identifiers must be distinct.")
        return self


def validate_structure(draft: StoryDraft | StoryPlan, request: StoryInput) -> None:
    """Validate durable identities without retroactively regrading old content."""
    if request.image_count == "auto":
        if request.max_images is None or not 1 <= len(draft.scenes) <= request.max_images:
            raise ValueError("The selected scene count is outside the allowed range.")
    elif len(draft.scenes) != request.image_count:
        raise ValueError("The scene count differs from the requested image count.")
    keys = [character.key for character in draft.characters]
    if len(set(keys)) != len(keys):
        raise ValueError("Character keys must be distinct.")
    for scene in draft.scenes:
        if not scene.source_refs or not set(scene.source_refs) <= request.paragraphs.keys():
            raise ValueError("Scene source references are invalid.")
        visible = (
            scene.characters
            if isinstance(scene, Scene)
            else [subject.character for subject in scene.subjects]
        )
        if not set(visible) <= set(keys) or len(set(visible)) != len(visible):
            raise ValueError("Scene character references are invalid.")


def validate_visual_references(draft: StoryDraft | StoryPlan) -> None:
    """Locate invalid drawable references without exposing user text in errors."""
    issues: list[PlanningIssue] = []
    explanations: list[str] = []

    def reject(path: tuple[str | int, ...], explanation: str) -> None:
        issues.append(PlanningIssue(code="invalid_visual_reference", path=path))
        explanations.append(explanation)

    prop_keys = [prop.key for prop in draft.props]
    if len(set(prop_keys)) != len(prop_keys):
        reject(("props",), "Prop keys must be distinct.")
    character_keys = {character.key for character in draft.characters}
    for index in range(len(draft.scenes)):
        scene = draft.scenes[index]
        path: tuple[str | int, ...] = ("scenes", index)
        if scene.subjects is None:
            reject((*path, "subjects"), "New scenes require explicit visible subjects.")
            continue
        visible = [subject.character for subject in scene.subjects]
        if len(set(visible)) != len(visible) or not set(visible) <= character_keys:
            reject((*path, "subjects"), "Visible subject references are invalid.")
        if isinstance(scene, Scene) and visible != scene.characters:
            reject((*path, "characters"), "Visible subjects differ from retained references.")
        used = [state.prop for state in scene.props]
        if len(set(used)) != len(used):
            reject((*path, "props"), "Visible props must be distinct.")
        for position, state in enumerate(scene.props):
            if state.prop not in prop_keys:
                reject((*path, "props", position, "prop"), "Declare the referenced prop first.")
            if state.holder is not None and state.holder not in visible:
                reject(
                    (*path, "props", position, "holder"),
                    "A prop holder must be visible in the scene.",
                )
    if issues:
        raise PlanningValidationError(
            "invalid_visual_reference", " ".join(explanations), issues=issues
        )


def validate_draft(draft: StoryDraft, request: StoryInput) -> None:
    """Report all deterministic defects together within the single repair budget."""
    issues: list[PlanningIssue] = []
    explanations: list[str] = []

    def reject(code: str, explanation: str) -> None:
        issues.append(PlanningIssue(code=code))
        explanations.append(explanation)

    checks: tuple[tuple[Callable[[], None], str], ...] = (
        (lambda: validate_structure(draft, request), "invalid_structure"),
        (lambda: validate_visual_references(draft), "invalid_visual_reference"),
    )
    for check, code in checks:
        try:
            check()
        except PlanningValidationError as error:
            issues.extend(error.issues)
            explanations.append(str(error))
        except ValueError as error:
            reject(code, str(error))
    if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", request.source_text) and any(
        not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", scene.narrative) for scene in draft.scenes
    ):
        reject(
            "narrative_language",
            "Write narrative, titles and synopsis in Japanese; keep visual fields in English.",
        )
    visual_fields: list[tuple[tuple[str | int, ...], str]] = [
        (("characters", index, "appearance"), character.appearance)
        for index, character in enumerate(draft.characters)
    ]
    visual_fields += [
        (("props", index, "appearance"), prop.appearance) for index, prop in enumerate(draft.props)
    ]
    for index in range(len(draft.scenes)):
        scene = draft.scenes[index]
        visual_fields += [
            (("scenes", index, "visual_description"), scene.visual_description),
            (("scenes", index, "location"), scene.location or ""),
        ]
        visual_fields += [
            (("scenes", index, "subjects", n, "action"), subject.action)
            for n, subject in enumerate(scene.subjects)
        ]
        visual_fields += [
            (("scenes", index, "props", n, "state"), state.state)
            for n, state in enumerate(scene.props)
        ]
    for path, text in visual_fields:
        if re.search(r"(?<![A-Za-z0-9_])[cop][1-9][0-9]*(?![A-Za-z0-9_])", text):
            issues.append(PlanningIssue(code="reference_in_visual_text", path=path))
            explanations.append(
                "Use visible English descriptions in visual prose; keep IDs in reference fields."
            )
    for path, text in visual_fields:
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text):
            issues.append(PlanningIssue(code="visual_language", path=path))
            explanations.append(
                "Use English visual prose and Japanese narrative, titles and synopsis."
            )
    descriptions = [
        (
            " ".join(scene.visual_description.split()).casefold(),
            (scene.location or "").casefold(),
            tuple((subject.character, subject.action.casefold()) for subject in scene.subjects),
            tuple(
                (state.prop, state.holder, state.visible, state.state.casefold())
                for state in scene.props
            ),
        )
        for scene in draft.scenes
    ]
    if len(set(descriptions)) != len(descriptions):
        reject("duplicate_visual", "Every scene must depict a distinct visual moment.")
    narratives = [" ".join(scene.narrative.split()) for scene in draft.scenes]
    if len(set(narratives)) != len(narratives):
        reject(
            "duplicate_narrative", "Each scene must advance the narrative without repeated prose."
        )
    if {ref for scene in draft.scenes for ref in scene.source_refs} != set(request.paragraphs):
        reject("source_coverage", "Every source paragraph must be represented by the plan.")
    if issues:
        raise PlanningValidationError(
            issues[0].code, " ".join(dict.fromkeys(explanations)), issues=issues
        )
