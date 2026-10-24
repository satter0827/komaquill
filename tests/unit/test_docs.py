"""Validate authored document contracts without checking prose style."""

import pytest

from scripts import check_docs


def tree(root):
    for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md"):
        (root / name).write_text("# Start\n")
    registry = root / "docs/requirements/catalog.toml"
    registry.parent.mkdir(parents=True)
    registry.write_text("""format_version = 1
[requirements.REQ-ONE]
summary = "Generate"
status = "planned"
acceptance = ["Save"]
sources = []
tests = []
""")
    return registry


def test_local_links_fragments_and_fenced_examples(tmp_path):
    tree(tmp_path)
    (tmp_path / "README.md").write_text(
        "# Start\n[link](CONTRIBUTING.md#start)\n```\n[example](missing)\n```\n"
        "[external](https://example.com/missing)\n"
    )
    assert check_docs.validate(tmp_path) == []
    assert check_docs.anchors("## Same\n## Same\n## 日本語\n") == {"same", "same-1", "日本語"}
    check_docs.main(tmp_path)


@pytest.mark.parametrize("target", ["missing.md", "../outside.md", "CONTRIBUTING.md#missing"])
def test_invalid_links_are_rejected(tmp_path, target):
    tree(tmp_path)
    (tmp_path / "README.md").write_text(f"[link]({target})")
    assert check_docs.validate(tmp_path)
    with pytest.raises(SystemExit):
        check_docs.main(tmp_path)


def test_implemented_requirement_requires_existing_code_and_tests(tmp_path):
    registry = tree(tmp_path)
    registry.write_text(registry.read_text().replace('"planned"', '"implemented"'))
    assert set(check_docs.validate(tmp_path)) == {"REQ-ONE:sources", "REQ-ONE:tests"}
    for name in ("src/feature.py", "tests/test_feature.py"):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("pass")
    registry.write_text(
        registry.read_text()
        .replace("sources = []", 'sources = ["src/feature.py"]')
        .replace("tests = []", 'tests = ["tests/test_feature.py"]')
    )
    assert check_docs.validate(tmp_path) == []
    (tmp_path / "tests/test_feature.py").unlink()
    assert check_docs.validate(tmp_path) == ["REQ-ONE:tests"]


def test_invalid_registry_contracts_and_missing_entrypoints(tmp_path):
    registry = tree(tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    text = (
        registry.read_text()
        .replace("format_version = 1", "format_version = 2")
        .replace("REQ-ONE", "bad")
        .replace('"planned"', '"unknown"')
        .replace('acceptance = ["Save"]', "acceptance = []")
        .replace("sources = []", 'sources = ["../outside.py"]')
    )
    registry.write_text(text)
    errors = check_docs.validate(tmp_path)
    assert {
        "AGENTS.md",
        "requirements:format",
        "requirements:identity",
        "bad:status",
        "bad:acceptance",
        "bad:sources",
    } <= set(errors)
