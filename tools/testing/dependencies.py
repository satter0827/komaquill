"""Read a repository import graph in an isolated interpreter without product imports."""

import ast
import json
import sys
from pathlib import Path

import grimp


def dependents(root: Path, changes: list[str]) -> list[str] | None:
    sys.path[:0] = [str(root / "src/backend/src"), str(root)]
    packages = [
        name
        for name in ("komaquill", "tests", "tools", "scripts", "bootstrap")
        if (
            (root / "src/backend/src" if name == "komaquill" else root) / name / "__init__.py"
        ).is_file()
    ]
    if not packages:
        return None
    for folder in ("src/backend/src", "tests", "tools", "scripts", "bootstrap"):
        for path in (root / folder).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # The library handles static imports. Dynamic imports require a full run.
            if any(
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    or isinstance(node.func, ast.Name)
                    and node.func.id in {"__import__", "import_module"}
                )
                for node in ast.walk(tree)
            ):
                return None
    graph = grimp.build_graph(*packages, include_external_packages=True, cache_dir=None)
    selected: set[str] = set()
    for name in changes:
        if not name.endswith(".py"):
            continue
        module = (
            name.removeprefix("src/backend/src/")
            .removesuffix(".py")
            .replace("/", ".")
            .removesuffix(".__init__")
        )
        if module not in graph.modules:
            return None
        selected.add(module)
        selected.update(
            graph.find_downstream_modules(module, as_package=name.endswith("/__init__.py"))
        )
    return sorted(selected)


if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        result = dependents(Path(request["root"]), request["changes"])
    except (OSError, ValueError, SyntaxError, ImportError):
        result = None
    json.dump(result, sys.stdout)
