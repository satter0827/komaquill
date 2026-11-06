"""Verify platform contracts within the declared test scope."""

import ast
import json
from pathlib import Path
from string import Formatter

from komaquill.adapters import provenance
from komaquill.domain.errors import ErrorCode
from komaquill.resources import messages

ROOT = Path(__file__).resolve().parents[4]


def test_catalog_keys_are_unique_and_error_categories_are_explained():
    def unique(pairs):
        result = {}
        for key, value in pairs:
            assert key not in result
            result[key] = value
        return result

    data = json.loads(
        (ROOT / "src/backend/src/komaquill/resources/messages.json").read_text(encoding="utf-8"),
        object_pairs_hook=unique,
    )
    assert all("error." + code.value in data for code in ErrorCode)
    assert all(isinstance(value, str) and value.strip() for value in data.values())


def test_literal_message_references_and_named_values_match_the_catalog():
    data = messages.catalog()
    for path in [
        *(ROOT / "src/backend/src").rglob("*.py"),
        *(ROOT / "scripts").rglob("*.py"),
        *(ROOT / "tools").rglob("*.py"),
        *(ROOT / "bootstrap").rglob("*.py"),
    ]:
        data = (
            json.loads((ROOT / "scripts/resources/messages.json").read_bytes())
            if any(path.is_relative_to(ROOT / name) for name in ("tools", "scripts", "bootstrap"))
            else messages.catalog()
        )
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "message" and node.args:
                key = node.args[0]
                if isinstance(key, ast.Constant):
                    assert key.value in data, (path, key.value)
                    required = {
                        n for _, n, _, _ in Formatter().parse(data[key.value]) if n is not None
                    }
                    assert required == {k.arg for k in node.keywords}, (path, key.value)
            for kw in node.keywords:
                if kw.arg == "display_key" and isinstance(kw.value, ast.Constant):
                    assert kw.value.value in data, (path, kw.value.value)


def test_extension_catalog_and_executable_resources_are_packaged():
    import re

    package = ROOT / "src/backend/src/komaquill"
    scripts = list((package / "interfaces/comfyui/web").glob("*.js"))
    assert scripts
    for path in scripts:
        name = path.relative_to(package).as_posix()
        source = path.read_text(encoding="utf-8")
        for key in re.findall(r'"(ui\.[a-z_]+)"', source):
            assert key in messages.catalog()
        assert "komaquill/" + name in dict(provenance.gpu_contents())
