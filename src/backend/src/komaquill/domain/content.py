"""Define content visibility independently of generation and storage."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

ContentKind = Literal["conversation", "preset", "image", "archive", "job"]
ContentRating = Literal["general", "nsfw", "unclassified"]


class ContentLabel(BaseModel):
    readable: bool = Field(
        default=True,
        description="Whether the saved display and classification metadata can be read.",
    )
    kind: ContentKind
    id: UUID
    rating: ContentRating
    revision: int = 0
    title: str = ""


class ContentUpdate(BaseModel):
    operation_id: UUID
    kind: ContentKind
    id: UUID
    rating: Literal["general", "nsfw"]
    expected_revision: int = Field(ge=0)


def visible(rating: ContentRating, nsfw_enabled: bool) -> bool:
    return nsfw_enabled or rating == "general"
