"""Keep ownership and verification scope visible in the repository hierarchy."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_python_package_has_layers_and_explicit_entrypoints():
    package = ROOT / "src/backend/src/komaquill"
    assert {p.name for p in package.glob("*.py")} == {"__init__.py", "__main__.py"}
    assert {
        p.name
        for p in package.iterdir()
        if p.is_dir()
        and any(q.suffix == ".py" and "__pycache__" not in q.parts for q in p.rglob("*.py"))
    } == {
        "api",
        "application",
        "domain",
        "adapters",
        "composition",
        "interfaces",
        "worker",
        "resources",
    }


def test_test_paths_declare_target_scope_and_feature():
    scopes = {"unit", "integration", "structure", "e2e"}
    owners = {"backend", "frontend", "tools", "scripts", "bootstrap", "system"}
    for path in (ROOT / "tests").rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        name = path.name
        if not (
            name.startswith("test_")
            and path.suffix == ".py"
            or name.endswith((".test.mjs", ".test.ts", ".test.tsx", ".spec.ts"))
        ):
            continue
        target, scope, *rest = path.relative_to(ROOT / "tests").parts
        assert target in owners and scope in scopes, path
        assert len(rest) >= 2, path


def test_frozen_migration_does_not_import_current_business_or_schema():
    import ast

    path = (
        ROOT
        / "src/backend/src/komaquill/adapters/persistence/migrations/versions"
        / "0012_storage_boundaries.py"
    )
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("komaquill")


def test_current_metadata_matches_the_frozen_schema():
    import json

    from komaquill.adapters.persistence.schema import metadata
    from sqlalchemy.dialects.sqlite import dialect
    from sqlalchemy.schema import CreateIndex, CreateTable

    actual = [
        {
            "name": table.name,
            "create": str(CreateTable(table).compile(dialect=dialect())),
            "columns": [column.name for column in table.columns if column.computed is None],
            "indexes": [
                str(CreateIndex(index).compile(dialect=dialect()))
                for index in sorted(table.indexes, key=lambda index: index.name)
            ],
        }
        for table in metadata.sorted_tables
    ]
    frozen = ROOT / "src/backend/src/komaquill/adapters/persistence/migrations/schema_v2.json"
    assert actual == json.loads(frozen.read_bytes())
