"""Adapt LangChain's fake and OpenAI-compatible models behind the planning port."""

import hashlib
import json
import os
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

import keyring
from pydantic import SecretStr

from komaquill.domain.errors import KomaquillError
from komaquill.domain.stories import PlannerProvenance, PlanningResponse, StoryInput
from komaquill.infrastructure.configuration import Project
from komaquill.infrastructure.stories.configuration import StorySettings, instructions


class LangChainPlanner:
    """Load optional SDKs lazily and suppress external tracing for every call."""

    def __init__(self, provenance: PlannerProvenance, model: Any = None) -> None:
        self.provenance = provenance
        self.model = model

    def complete(
        self, request: StoryInput, schema: dict[str, object], correction: str | None
    ) -> PlanningResponse:
        try:
            from langchain_core.language_models.fake import FakeListLLM
            from langchain_core.prompts import ChatPromptTemplate
            from langsmith import tracing_context
        except ImportError:
            raise KomaquillError(code="llm_dependencies_missing", stage="story_planning") from None
        paragraphs = list(request.paragraphs)
        count = request.image_count
        guide = [
            {
                "position": index + 1,
                "source_refs": paragraphs[
                    index * len(paragraphs) // count : max(
                        (index + 1) * len(paragraphs) // count, index * len(paragraphs) // count + 1
                    )
                ],
            }
            for index in range(count)
        ]
        payload = {
            "scene_guide": guide,
            "image_count": request.image_count,
            "source_paragraphs": request.paragraphs,
            "schema": schema,
            "correction": correction,
        }
        with tracing_context(enabled=False):
            template = ChatPromptTemplate.from_messages(
                [
                    ("system", "{instructions}"),
                    ("human", "{request}"),
                ]
            )
            prompt = template.format_prompt(
                instructions=self.provenance.instructions,
                request=json.dumps(payload, ensure_ascii=False),
            )
            if self.provenance.provider == "fake":
                example = files("komaquill").joinpath("resources/stories/examples/rainy-cat")
                expected = example.joinpath("source.md").read_text(encoding="utf-8")
                if request.source_text.replace("\r\n", "\n").strip() != expected.strip():
                    raise KomaquillError(
                        code="fake_story_mismatch", stage="story_planning", retryable=False
                    )
                name = f"plan-{request.image_count}.json"
                if not example.joinpath(name).is_file():
                    raise KomaquillError(
                        code="fake_story_mismatch", stage="story_planning", retryable=False
                    )
                fake = self.model or FakeListLLM(
                    responses=[example.joinpath(name).read_text(encoding="utf-8")],
                    cache=False,
                )
                return PlanningResponse(
                    content=fake.invoke(prompt), model="fake-list", finish_reason="stop"
                )
            try:
                result = self.model.bind(
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "story_plan", "strict": True, "schema": schema},
                    }
                ).invoke(prompt, config={"callbacks": []})
            except Exception as error:
                from openai import LengthFinishReasonError

                if isinstance(error, LengthFinishReasonError):
                    choice = error.completion.choices[0]
                    return PlanningResponse(
                        content=choice.message.content or "",
                        model=error.completion.model,
                        finish_reason="length",
                    )
                status = getattr(error, "status_code", None)
                code = "authentication_failed" if status in {401, 403} else "llm_unavailable"
                raise KomaquillError(
                    code=code,
                    stage="story_planning",
                    http_status=status if isinstance(status, int) else None,
                ) from None
        refusal = result.additional_kwargs.get("refusal")
        actual = result.response_metadata.get("model_name") or result.response_metadata.get("model")
        return PlanningResponse(
            refusal=bool(refusal) or not isinstance(result.content, str),
            content=(result.content or str(refusal))
            if isinstance(result.content, str)
            else json.dumps(result.content, ensure_ascii=False),
            model=actual,
            finish_reason=result.response_metadata.get("finish_reason"),
        )


def planner(project: Project, options: StorySettings, *, allow_real_llm: bool) -> LangChainPlanner:
    """Check explicit consent before importing clients or resolving credentials."""
    profile = options.llm_profiles[options.llm_profile]
    if profile.provider != "fake" and not allow_real_llm:
        raise KomaquillError(code="real_llm_not_allowed", stage="story_planning", retryable=False)
    text = instructions(project, options)
    provenance = PlannerProvenance(
        profile=options.llm_profile,
        provider=profile.provider,
        configured_model=profile.model,
        instructions=text,
        instruction_sha256=hashlib.sha256(text.encode()).hexdigest(),
        timeout_seconds=options.request_timeout,
        max_output_tokens=options.max_output_tokens,
        require_model_match=profile.require_model_match,
        base_url=profile.base_url,
        reasoning_effort=profile.reasoning_effort,
        temperature=profile.temperature,
        app_identity=project.identity(),
    )
    if profile.provider == "fake":
        return LangChainPlanner(provenance)
    try:
        from langchain_openai import ChatOpenAI
        from langsmith import tracing_context
    except ImportError:
        raise KomaquillError(code="llm_dependencies_missing", stage="story_planning") from None
    key = os.environ.get(profile.key_env) if profile.key_env else None
    if key is None and profile.credential_ref is not None:
        key = keyring.get_password("komaquill.llm", profile.credential_ref)
    local = urlsplit(profile.base_url or "").hostname in {"localhost", "127.0.0.1", "::1"}
    if key is None:
        if not local:
            raise KomaquillError(
                code="authentication_failed", stage="story_planning", retryable=False
            )
        key = "lm-studio"
    with tracing_context(enabled=False):
        model = ChatOpenAI(
            model=profile.model,
            base_url=profile.base_url,
            api_key=SecretStr(key),
            timeout=options.request_timeout,
            max_completion_tokens=options.max_output_tokens,
            max_retries=0,
            reasoning_effort=profile.reasoning_effort,
            temperature=profile.temperature,
            use_responses_api=False,
            stream_usage=False,
            cache=False,
        )
    return LangChainPlanner(provenance, model)
