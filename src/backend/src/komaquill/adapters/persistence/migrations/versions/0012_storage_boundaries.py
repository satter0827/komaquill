"""Replace application tables with constrained relationships and object references."""

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    existing = set(sa.inspect(connection).get_table_names())
    replaced = []
    tables = json.loads(Path(__file__).parents[1].joinpath("schema_v2.json").read_bytes())
    for table in tables:
        name = table["name"]
        if name in existing:
            old = "_source_" + name
            connection.exec_driver_sql(f'ALTER TABLE "{name}" RENAME TO "{old}"')
            replaced.append(old)
            # Index names are database-wide, including indexes on the renamed source.
            for index in sa.inspect(connection).get_indexes(old):
                connection.exec_driver_sql(f'DROP INDEX "{index["name"]}"')
            connection.exec_driver_sql(table["create"])
            names = table["columns"]
            quoted = ",".join(f'"{name}"' for name in names)
            if name == "operations":
                connection.exec_driver_sql(
                    f'INSERT INTO archived_operations ({quoted}) SELECT {quoted} FROM "{old}" '
                    "WHERE conversation_id NOT IN (SELECT id FROM conversations)"
                )
            condition = (
                " WHERE conversation_id IN (SELECT id FROM conversations)"
                if name == "operations"
                else ""
            )
            connection.exec_driver_sql(
                f'INSERT INTO "{name}" ({quoted}) SELECT {quoted} FROM "{old}"{condition}'
            )
        else:
            connection.exec_driver_sql(table["create"])
        for statement in table["indexes"]:
            connection.exec_driver_sql(statement)
    for old in reversed(replaced):
        connection.exec_driver_sql(f'DROP TABLE "{old}"')


def downgrade() -> None:
    raise RuntimeError("Restore the verified workspace backup to recover the previous schema.")
