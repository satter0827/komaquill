"""Verify workflow exports and package structure."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_browser_modules_use_comfyuis_uncached_javascript_routes():
    import re
    from importlib.resources import files

    web = files("komaquill").joinpath("interfaces/comfyui/web")
    modules = list(web.iterdir())
    assert modules and all(module.name.endswith(".js") for module in modules)
    for module in modules:
        for relative in re.findall(r'from "(\./[^"]+)"', module.read_text(encoding="utf-8")):
            assert web.joinpath(relative.removeprefix("./")).is_file()
