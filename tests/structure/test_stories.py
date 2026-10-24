"""Verify local planning and explicit browser boundaries."""

import ast
import inspect

from komaquill.infrastructure import provenance
from komaquill.interfaces.cli import start


def test_story_resources_and_clients_are_local_only():
    entries = dict(provenance.gpu_contents())
    assert not any("/stories/" in name for name in entries)
    for name, content in entries.items():
        if not name.endswith(".py"):
            continue
        for node in ast.walk(ast.parse(content)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            assert not any(
                value.startswith(("langchain", "langsmith", "openai")) for value in names
            )


def test_start_only_opens_a_browser_when_explicitly_requested():
    assert inspect.signature(start).parameters["open_browser"].default is False


def test_history_repositories_do_not_depend_on_readable_publication():
    from komaquill.infrastructure import storage
    from komaquill.infrastructure.stories import storage as story_storage

    for module in (storage, story_storage):
        imports = [
            node.module or ""
            for node in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(node, ast.ImportFrom)
        ]
        assert "komaquill.infrastructure.stories.publication" not in imports
