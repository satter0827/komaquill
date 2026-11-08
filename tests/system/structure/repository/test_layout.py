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


def test_frontend_test_execution_has_one_owner_in_the_effective_nx_graph():
    import json
    import os
    import subprocess

    projects = {}
    for name in ("frontend", "frontend-tests"):
        result = subprocess.run(
            ["node", "tools/runtime/nx.mjs", "show", "project", name, "--json"],
            cwd=ROOT,
            env={**os.environ, "NX_DAEMON": "false"},
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
        )
        projects[name] = json.loads(result.stdout)
    assert "test" not in projects["frontend"]["targets"]
    assert "test" in projects["frontend-tests"]["targets"]
    assert "frontend" in projects["frontend-tests"]["implicitDependencies"]


def test_browser_tests_track_the_scripts_they_execute_in_the_effective_nx_graph():
    import json
    import os
    import subprocess

    result = subprocess.run(
        ["node", "tools/runtime/nx.mjs", "show", "project", "e2e", "--json"],
        cwd=ROOT,
        env={**os.environ, "NX_DAEMON": "false"},
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )
    project = json.loads(result.stdout)
    assert "scripts" in project["implicitDependencies"]


def test_pipeline_deadline_outlives_python_and_browser_suite_deadlines(tmp_path):
    import json
    import os

    from tools.reporting import policy
    from tools.runtime.processes import run_owned

    output = tmp_path / "web-config.json"
    with (tmp_path / "web-config.log").open("w", encoding="utf-8") as stream:
        assert (
            run_owned(
                [
                    "node",
                    "--input-type=module",
                    "-e",
                    "import {writeFileSync} from 'node:fs'; "
                    "import {webConfig} from './tools/testing/web/config.mjs'; "
                    "writeFileSync(process.argv[1], JSON.stringify(webConfig()), {flag: 'wx'})",
                    str(output),
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "KOMAQUILL_WEB_EVIDENCE_DIR": str(tmp_path / "browser"),
                    "NO_COLOR": "1",
                    "FORCE_COLOR": "1",
                },
                stdout=stream,
                timeout=10,
            )
            == 0
        )
    browser = json.loads(output.read_text(encoding="utf-8"))
    limits = policy(ROOT)
    assert limits["pipeline_timeout"] > limits["step_timeout"] > 0
    assert limits["pipeline_timeout"] * 1000 > browser["globalTimeout"] > 0


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


def test_current_metadata_matches_the_migrated_database(tmp_path):
    from komaquill.adapters.persistence.connection import Database
    from komaquill.adapters.persistence.schema import metadata
    from sqlalchemy import inspect
    from sqlalchemy.dialects.sqlite import dialect

    storage = Database(tmp_path / "schema.sqlite3", create=True)
    try:
        with storage.engine.connect() as connection:
            inspector = inspect(connection)
            for table in metadata.sorted_tables:
                assert [
                    (column["name"], str(column["type"]), column["nullable"])
                    for column in inspector.get_columns(table.name)
                ] == [
                    (column.name, column.type.compile(dialect=dialect()), column.nullable)
                    for column in table.columns
                ], table.name
                assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
                    column.name for column in table.primary_key.columns
                ], table.name
                assert {
                    (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                    for index in inspector.get_indexes(table.name)
                } == {
                    (index.name, tuple(column.name for column in index.columns), bool(index.unique))
                    for index in table.indexes
                }, table.name
    finally:
        storage.engine.dispose()
