"""Exercise the OpenAI-compatible adapter against a local fake API."""

import builtins
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files

import pytest

from komaquill.application.stories.planning import plan_story
from komaquill.domain.errors import KomaquillError
from komaquill.infrastructure.stories.configuration import settings
from komaquill.infrastructure.stories.llm import planner
from komaquill.infrastructure.stories.storage import FileStoryRepository
from tests.support.stories import request


@pytest.fixture
def model_server():
    state = {
        "requests": [],
        "status": 200,
        "model": "sdk-test",
        "finish_reason": "stop",
        "refusal": None,
    }
    valid = json.loads(
        files("komaquill").joinpath("resources/stories/examples/rainy-cat/plan-4.json").read_bytes()
    )
    state["content"] = json.dumps(valid, ensure_ascii=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(
                {
                    "path": self.path,
                    "payload": payload,
                    "authorization": self.headers.get("Authorization"),
                }
            )
            value = {
                "id": "local-completion",
                "object": "chat.completion",
                "created": 1,
                "model": state["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": state["finish_reason"],
                        "message": {
                            "role": "assistant",
                            "content": state["content"],
                            "refusal": state["refusal"],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 20, "total_tokens": 40},
            }
            if state["status"] != 200:
                value = {"error": {"message": "private-body-must-not-leak", "type": "test_error"}}
            content = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def options(project, url, **changes):
    base = settings(project)
    values = base.model_dump()
    values["llm_profiles"]["lmstudio-4b"].update({"model": "sdk-test", "base_url": url, **changes})
    return type(base).model_validate(values)


def test_actual_adapter_transmits_schema_and_source_but_not_ambient_destinations(
    project, model_server, monkeypatch
):
    url, state = model_server
    monkeypatch.setenv("OPENAI_BASE_URL", "https://forbidden.invalid/v1")
    monkeypatch.setenv("OPENAI_API_BASE", "https://forbidden.invalid/v1")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", url + "/traces")
    monkeypatch.setenv("LANGSMITH_API_KEY", "not-a-real-key")
    monkeypatch.setenv("KOMAQUILL_LLM_API_KEY", "not-a-real-key")
    value = request()
    repository = FileStoryRepository(project)
    result = plan_story(
        repository,
        planner(project, options(project, url), allow_real_llm=True),
        value,
        allow_real_llm=True,
        max_images=8,
    )
    assert result.planner.provider == "openai-compatible"
    assert len(state["requests"]) == 1
    actual = state["requests"][0]
    assert actual["path"] == "/v1/chat/completions"
    assert actual["authorization"] == "Bearer not-a-real-key"
    payload = actual["payload"]
    assert payload["model"] == "sdk-test"
    assert payload["max_completion_tokens"] == 4096
    assert payload["reasoning_effort"] == "none" and payload["temperature"] == 0.3
    assert result.planner.base_url == url
    assert result.planner.reasoning_effort == "none"
    assert (
        payload["response_format"]["json_schema"]["schema"]["properties"]["scenes"]["minItems"] == 4
    )
    human = json.loads(payload["messages"][1]["content"])
    assert human["source_paragraphs"] == value.paragraphs and human["image_count"] == 4
    assert [s["source_refs"] for s in human["scene_guide"]] == [["p1"], ["p2"], ["p3"], ["p4"]]
    stored = "".join(path.read_text(encoding="utf-8") for path in repository.root.rglob("*.json"))
    assert "not-a-real-key" not in stored
    assert result.planner.app_identity["code_sha256"]
    assert not any("/traces" in item["path"] for item in state["requests"])


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication_failed"),
        (403, "authentication_failed"),
        (400, "llm_unavailable"),
        (429, "llm_unavailable"),
        (500, "llm_unavailable"),
    ],
)
def test_sdk_failures_are_safe_and_do_not_retry_http(project, model_server, status, code):
    url, state = model_server
    state["status"] = status
    repository = FileStoryRepository(project)
    value = request()
    with pytest.raises(KomaquillError) as error:
        plan_story(
            repository,
            planner(project, options(project, url), allow_real_llm=True),
            value,
            allow_real_llm=True,
            max_images=8,
        )
    assert error.value.code == code and error.value.http_status == status
    assert len(state["requests"]) == 1
    record = json.loads(next(repository.root.rglob("01.json")).read_bytes())
    assert record["failure"]["code"] == code
    assert "private-body" not in json.dumps(record)


@pytest.mark.parametrize("kind", ["model", "refusal", "truncated"])
def test_invalid_or_different_model_responses_do_not_generate_plans(project, model_server, kind):
    url, state = model_server
    if kind == "model":
        state["model"] = "wrong-9b-model"
    elif kind == "refusal":
        state["refusal"] = "private-refusal"
    else:
        state["finish_reason"] = "length"
    with pytest.raises(KomaquillError):
        plan_story(
            FileStoryRepository(project),
            planner(project, options(project, url), allow_real_llm=True),
            request(),
            allow_real_llm=True,
            max_images=8,
            repair_attempts=0,
        )
    assert len(state["requests"]) == 1
    attempt = json.loads(next(FileStoryRepository(project).root.rglob("01.json")).read_bytes())
    assert attempt["response"] is not None
    assert attempt["response"]["model"] == state.get("model", "sdk-test")


def test_provider_aliases_are_recorded_when_exact_matching_is_disabled(project, model_server):
    url, state = model_server
    state["model"] = "sdk-test-snapshot"
    repository = FileStoryRepository(project)
    value = request()
    plan_story(
        repository,
        planner(project, options(project, url, require_model_match=False), allow_real_llm=True),
        value,
        allow_real_llm=True,
        max_images=8,
    )
    attempt = json.loads(next(repository.root.rglob("01.json")).read_bytes())
    assert attempt["response"]["model"] == "sdk-test-snapshot"


def test_missing_credentials_are_rejected_before_cloud_requests(project, monkeypatch):
    monkeypatch.delenv("KOMAQUILL_LLM_API_KEY", raising=False)
    with pytest.raises(KomaquillError) as error:
        planner(project, options(project, "https://example.invalid/v1"), allow_real_llm=True)
    assert error.value.code == "authentication_failed"


def test_optional_dependencies_are_lazy_and_reported(project, monkeypatch):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name.startswith(("langchain_core", "langchain_openai")):
            raise ImportError
        return original(name, *args, **kwargs)

    fake = planner(project, settings(project, llm_profile="fake"), allow_real_llm=False)
    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(KomaquillError) as error:
        fake.complete(request(), {}, None)
    assert error.value.code == "llm_dependencies_missing"
    with pytest.raises(KomaquillError) as error:
        planner(project, settings(project), allow_real_llm=True)
    assert error.value.code == "llm_dependencies_missing"


def test_explicit_keyring_reference_is_used_without_recording_secret(
    project, model_server, monkeypatch
):
    url, state = model_server
    monkeypatch.delenv("KOMAQUILL_LLM_API_KEY", raising=False)
    from komaquill.infrastructure.stories import llm

    seen = []
    monkeypatch.setattr(
        llm.keyring,
        "get_password",
        lambda service, name: seen.append((service, name)) or "not-a-real-key",
    )
    value = planner(
        project, options(project, url, credential_ref="story-local"), allow_real_llm=True
    )
    value.complete(request(), {}, None)
    assert seen == [("komaquill.llm", "story-local")]
    assert state["requests"][0]["authorization"] == "Bearer not-a-real-key"
