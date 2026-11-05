"""Select one player without changing historical conversation content."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    payload = connection.execute(sa.text("SELECT payload FROM preferences WHERE id=1")).scalar()
    if payload is not None:
        configured = json.loads(payload)
        players: list[str] = list(
            connection.execute(sa.text("SELECT id FROM presets WHERE kind='player' AND deleted=0"))
            .scalars()
            .all()
        )
        chosen = configured.pop("player_id", None)
        if chosen not in players:
            if len(players) != 1:
                raise ValueError("Select a unique player before migrating.")
            chosen = players[0]
        connection.execute(
            sa.text("UPDATE presets SET deleted=1 WHERE kind='player' AND id!=:chosen"),
            {"chosen": chosen},
        )
        configured.pop("image_frequency", None)
        if configured.get("llm_profile") == "fake":
            configured["llm_profile"] = "lmstudio-4b"
        connection.execute(
            sa.text("UPDATE preferences SET payload=:payload WHERE id=1"),
            {"payload": json.dumps(configured, ensure_ascii=False)},
        )
    op.create_index(
        "one_active_player",
        "presets",
        ["kind"],
        unique=True,
        sqlite_where=sa.text("kind='player' AND deleted=0"),
    )


def downgrade() -> None:
    raise RuntimeError("Restore the recorded database backup instead of downgrading.")
