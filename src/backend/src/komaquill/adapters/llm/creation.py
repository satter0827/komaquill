"""Draft reusable character and story definitions."""

from typing import cast

from pydantic import BaseModel, Field

from komaquill.adapters.llm.execution import StructuredExecution
from komaquill.adapters.llm.language import check_language
from komaquill.domain.library.models import CharacterInput, CreationRequest, StoryInput


class StoryDraft(StoryInput):
    opening: str = Field(min_length=1, max_length=4000)
    character_id: None = None


class CharacterDraft(CharacterInput):
    age: str = Field(min_length=1, max_length=120, description="Requested age, or unknown.")
    gender: str = Field(min_length=1, max_length=120, description="Requested gender, or unknown.")
    appearance: str = Field(min_length=1, max_length=3000)
    personality: str = Field(min_length=1, max_length=3000)


class DraftGenerator:
    def __init__(self, execution: StructuredExecution) -> None:
        self.execution = execution

    def draft(self, request: CreationRequest) -> CharacterInput | StoryInput:
        language = "Japanese" if request.locale == "ja" else "English"
        schema = CharacterDraft if request.kind == "character_draft" else StoryDraft

        def validate(value: BaseModel) -> None:
            names = tuple(
                str(name) for name in value.model_dump(include={"name", "address"}).values() if name
            )
            check_language(value.model_dump_json(), request.locale, names)
            for field, text in value.model_dump().items():
                if (
                    field not in {"name", "address"}
                    and isinstance(text, str)
                    and text.startswith("{")
                    and text.endswith("}")
                ):
                    raise ValueError("literal_field_values")

        result = self.execution.invoke(
            schema,
            request.operation_id,
            request.llm_profile,
            request.allow_real_llm,
            f"Create a concise {request.kind} from the user's directions. Write visible fields in "
            f"{language}. Preserve requested age, gender, appearance, names and roles "
            "in their matching fields. "
            "For a character provide usable appearance and personality; "
            "for a story provide an opening scene. "
            "Do not discard provided facts. Set age and gender to the specified values; "
            "if not provided, use the localized word for unknown. "
            "Leave other unspecified personal facts empty. "
            "Use plain literal values, never template braces or placeholder syntax. "
            "Choose a fitting story title unless the user supplies one. "
            "Do not add identifiers or hidden reasoning.",
            [("user", request.prompt + f"\n\nWrite the draft in {language}.")],
            purpose="draft-",
            validate=validate,
            correction=(
                f"Write the draft fields in {language}. "
                "Use plain literal field values without template braces; "
                "preserve requested names and facts."
            ),
        )
        return cast(CharacterInput | StoryInput, result)
