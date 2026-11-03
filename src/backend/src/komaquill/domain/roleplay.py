"""Define people, scenery and explicit updates without transport or model dependencies."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhysicalObject(Record):
    id: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    name: str = Field(
        min_length=1,
        max_length=300,
        description=(
            "A physical item. Agreements, feelings and knowledge belong in notes, not here."
        ),
    )


def unique_objects(values: list[PhysicalObject]) -> list[PhysicalObject]:
    if len({value.id for value in values}) != len(values):
        raise ValueError("Physical object identities must be unique.")
    return values


PhysicalObjects = Annotated[
    list[PhysicalObject], Field(max_length=30), AfterValidator(unique_objects)
]


class PersonDefinition(Record):
    name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=6000)
    appearance: str = Field(default="", max_length=3000)
    role: str = Field(default="", max_length=1000)
    address: str = Field(default="", max_length=120)
    age: str = Field(default="", max_length=120)
    gender: str = Field(default="", max_length=120)
    initial_clothing: str | None = Field(default=None, max_length=3000)
    initial_held_objects: PhysicalObjects = Field(default_factory=list)
    visual_review_required: bool = False


class CharacterDefinition(PersonDefinition):
    personality: str = Field(default="", max_length=3000)
    speech: str = Field(default="", max_length=2000)
    background: str = Field(default="", max_length=4000)
    relationship: str = Field(default="", max_length=2000)


class StoryDefinition(Record):
    name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)
    world: str = Field(default="", max_length=6000)
    opening: str = Field(default="", max_length=4000)
    place: str = Field(default="", max_length=1000)
    goal: str = Field(default="", max_length=2000)


class PersonState(Record):
    appearance: str | None = Field(default=None, max_length=3000)
    clothing: str | None = Field(default=None, max_length=3000)
    expression: str | None = Field(default=None, max_length=1000)
    pose: str | None = Field(default=None, max_length=1000)
    held_objects: PhysicalObjects | None = Field(
        default=None,
        max_length=30,
        description=(
            "Physical objects currently held in this person's hands. "
            "Promises, wishes and knowledge are not objects."
        ),
    )


class ConversationState(Record):
    place: str = Field(default="", max_length=1000)
    time: str = Field(default="", max_length=1000)
    goal: str = Field(default="", max_length=2000)
    relationship: str = Field(default="", max_length=2000)
    character_place: str = Field(default="", max_length=1000)
    scenery: str = Field(default="", max_length=4000)
    intention: str = Field(default="", max_length=2000)
    elapsed_minutes: int = Field(default=0, ge=0)
    player: PersonState = Field(default_factory=PersonState)
    character: PersonState = Field(default_factory=PersonState)


class PersonStatePatch(Record):
    appearance: str | None = Field(default=None, max_length=3000)
    clothing: str | None = Field(default=None, max_length=3000)
    expression: str | None = Field(default=None, max_length=1000)
    pose: str | None = Field(default=None, max_length=1000)
    held_objects: PhysicalObjects | None = Field(
        default=None,
        max_length=30,
        description=(
            "Physical objects currently held in this person's hands. "
            "Promises, wishes and knowledge are not objects."
        ),
    )


class StatePatch(Record):
    """Omitted fields retain values; null clears a value; lists replace values."""

    place: str | None = Field(default=None, max_length=1000)
    time: str | None = Field(default=None, max_length=1000)
    goal: str | None = Field(default=None, max_length=2000)
    relationship: str | None = Field(default=None, max_length=2000)
    character_place: str | None = Field(default=None, max_length=1000)
    scenery: str | None = Field(default=None, max_length=4000)
    player: PersonStatePatch | None = None
    character: PersonStatePatch | None = None

    @classmethod
    def between(cls, previous: ConversationState, desired: ConversationState) -> Self:
        """Derive explicit changes while preserving unknown person properties."""
        old, current = previous.model_dump(), desired.model_dump()
        changes: dict[str, object] = {}
        for key, value in current.items():
            if key in {"player", "character"}:
                delta = {
                    name: item
                    for name, item in value.items()
                    if item is not None and item != old[key][name]
                }
                if delta:
                    changes[key] = delta
            elif value != old[key]:
                changes[key] = value
        return cls.model_validate(changes)

    def apply(self, previous: ConversationState) -> ConversationState:
        result = previous.model_dump()
        for key, value in self.model_dump(exclude_unset=True).items():
            if key in {"player", "character"}:
                if value is None:
                    result[key] = PersonState().model_dump()
                else:
                    result[key].update(
                        {
                            name: ([] if name == "held_objects" else "") if item is None else item
                            for name, item in value.items()
                        }
                    )
            else:
                result[key] = value if value is not None else ""
        return ConversationState.model_validate(result)


class ConversationMemory(Record):
    source_version: Literal[1, 2, 3, 4] = 1
    text: str = Field(max_length=2048)
    through_turn: int = Field(ge=1)
    source_sha256: str
    locale: str
    model: str
    instructions_sha256: str
