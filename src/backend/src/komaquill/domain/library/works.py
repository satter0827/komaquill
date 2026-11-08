"""Describe user-visible works independently of their source records."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from komaquill.domain.roleplay import Record

WorkKind = Literal["conversation", "archived", "character", "story", "image", "bookmark"]


class WorkRef(Record):
    kind: WorkKind
    id: UUID
    index: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def image_index(self) -> "WorkRef":
        if self.kind != "image" and self.index:
            raise ValueError("Only image outputs have an index.")
        return self


class WorkItem(WorkRef):
    name: str
    revision: int = Field(default=0, ge=0)
    trashed: bool = False
    owner: UUID | None = None


class WorkChange(Record):
    target: WorkRef
    operation_id: UUID
    expected_revision: int = Field(ge=0)
    expected_content_revision: int | None = Field(default=None, ge=0)
    action: Literal["rename", "trash", "restore"]
    name: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"\S")

    @model_validator(mode="after")
    def rename_has_name(self) -> "WorkChange":
        if (self.action == "rename") != (self.name is not None):
            raise ValueError("Supply a name only when renaming.")
        return self


class ImageUse(Record):
    kind: Literal["conversation", "character", "story"]
    owner: UUID
    slot: str
    name: str


class ImageDetach(Record):
    image: WorkRef
    use: ImageUse
