"""Reconstruct pre-checkpointer records for explicit migration fixtures."""

import json

from sqlalchemy import text


def legacy_work_tables(execute):
    execute("DROP TABLE work_metadata")
    execute("DROP TABLE image_detachments")


def legacy_execution(connection):
    legacy_work_tables(connection.exec_driver_sql)
    connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
    for identity, payload in connection.execute(text("SELECT id,payload FROM conversations")).all():
        try:
            value = json.loads(payload)
        except ValueError:
            continue
        if "turns" not in value:
            value["turns"] = [
                json.loads(row[0])
                for row in connection.execute(
                    text(
                        "SELECT payload FROM conversation_turns "
                        "WHERE conversation_id=:id ORDER BY position"
                    ),
                    {"id": identity},
                )
            ]
        for kind in ("actions", "proposals", "visual_reviews"):
            if kind not in value:
                value[kind] = [
                    json.loads(row[0])
                    for row in connection.execute(
                        text(
                            "SELECT payload FROM conversation_events "
                            "WHERE conversation_id=:id AND kind=:kind"
                        ),
                        {"id": identity, "kind": kind},
                    )
                ]
        connection.execute(
            text("UPDATE conversations SET payload=:payload WHERE id=:id"),
            {"id": identity, "payload": json.dumps(value, ensure_ascii=False)},
        )
    for table in (
        "operation_policies",
        "conversation_turns",
        "conversation_events",
        "conversation_bookmarks",
        "execution_migration_originals",
    ):
        connection.exec_driver_sql(f"DROP TABLE {table}")
    connection.exec_driver_sql("ALTER TABLE operations ADD COLUMN reply TEXT")
