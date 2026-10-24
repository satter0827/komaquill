"""Verify generation reuse without filesystem, HTTP or provider dependencies."""

from contextlib import nullcontext
from uuid import uuid4

from komaquill.application.generation import generate
from komaquill.domain.models import Session


class MemoryRepository:
    def __init__(self):
        self.rows = {}
        self.writes = 0

    def lock(self):
        return nullcontext()

    def load(self, unreadable=None):
        return list(self.rows.values())

    def read(self, identity):
        return self.rows[identity].model_copy(deep=True)

    def save(self, run):
        self.rows[run.run_id] = run.model_copy(deep=True)
        self.writes += 1

    def conditions(self, run, prompt):
        pass

    def prepare(self, run, graph, ui):
        pass

    def claim(self, identity):
        return nullcontext(True)


class MemoryArtifacts:
    def cached_image(self, key, previous):
        return None

    def inspect_image(self, content, reference):
        from komaquill.domain.models import ImageMetadata

        assert content == b"adapter-verified-image"
        return ImageMetadata(
            sha256="a" * 64, size_bytes=len(content), width=1, height=1, source=reference
        )

    def write(self, key, content):
        assert key.endswith("images/0001.png")


class MemoryBackend:
    def resolve_models(self, graph, ui):
        return graph, ui

    def __init__(self):
        self.record = None
        self.submissions = 0

    def submit(self, graph, ui, identity):
        self.submissions += 1
        self.record = {
            "prompt": [0, "prompt-id", graph, {"komaquill": {"run_id": identity}}],
            "status": {"completed": True},
            "outputs": {
                "9": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}
            },
        }
        return "prompt-id"

    def history(self):
        return {"prompt-id": self.record}

    def queue(self):
        return []

    def image(self, reference):
        return b"adapter-verified-image"


def test_generation_uses_only_replaceable_contracts():
    repository = MemoryRepository()

    class Workspace:
        def configured(self, settings=None):
            return self

        def settings(self):
            from komaquill.infrastructure.configuration import Settings

            return Settings(
                poll_interval=0.001,
                request_timeout=1.0,
                generation_timeout=1.0,
                preparation_timeout=1.0,
                shutdown_timeout=1.0,
                worker_execution_timeout=1.0,
                control_timeout=1.0,
                local_port=18188,
                remote_port=8188,
                gpu="T4",
            )

        def prepared_assets(self, session):
            return {}

        def identity(self):
            return {"app_version": "test", "source_revision": None, "code_sha256": None}

        def prepare(self, prompt, negative, seed, identity, profile_name=None):
            return {"prompt": prompt, "seed": seed}, {}

        def stop_requested(self, session):
            return None

    workspace = Workspace()
    workspace.repository = repository
    workspace.artifacts_store = MemoryArtifacts()
    workspace.accepts_results = lambda session: True
    backend = MemoryBackend()
    session = Session(endpoint="http://127.0.0.1:8188", runtime_id=uuid4(), backend="external")
    run = generate(workspace, backend, session, "a quiet lake", "", 1)
    assert run.state == "saved" and run.images == ["images/0001.png"]
    assert backend.submissions == 1 and repository.writes >= 3
    assert len(repository.rows) == 1
