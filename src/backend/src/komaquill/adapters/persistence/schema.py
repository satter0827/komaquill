"""Declare the application-owned SQLite tables in one place."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

metadata = MetaData()

archived_conversations = Table(
    "archived_conversations",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("reason", String, nullable=False),
)

archived_operations = Table(
    "archived_operations",
    metadata,
    Column("id", String, primary_key=True),
    Column("conversation_id", String, ForeignKey("archived_conversations.id"), nullable=False),
    Column("request", Text, nullable=False),
    Column("fingerprint", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("active_slot", Integer),
    Column("owner_pid", Integer, nullable=False),
    Column("owner_started", String, nullable=False),
    Column("base_revision", Integer),
)


conversations = Table(
    "conversations",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
    Column(
        "revision",
        Integer,
        Computed(
            "CASE WHEN json_valid(payload) THEN json_extract(payload, '$.revision') END",
            persisted=True,
        ),
    ),
    CheckConstraint(
        "json_valid(payload) AND json_extract(payload, '$.id') = id", name="conversation_identity"
    ),
    CheckConstraint("revision >= 0", name="conversation_revision"),
)


operations = Table(
    "operations",
    metadata,
    Column("id", String, primary_key=True),
    Column("conversation_id", String, ForeignKey("conversations.id"), nullable=False),
    Column("request", Text, nullable=False),
    Column("fingerprint", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("active_slot", Integer, unique=True),
    Column("owner_pid", Integer, nullable=False),
    Column("owner_started", String, nullable=False),
    Column("base_revision", Integer),
    Column(
        "state",
        String,
        Computed(
            "CASE WHEN json_valid(payload) THEN json_extract(payload, '$.state') END",
            persisted=True,
        ),
    ),
    CheckConstraint("json_valid(request) AND json_valid(payload)", name="operation_json"),
    CheckConstraint(
        "json_extract(payload, '$.id') = id "
        "AND json_extract(payload, '$.conversation_id') = conversation_id",
        name="operation_identity",
    ),
    CheckConstraint(
        "state IN ('accepted','running','complete','failed','interrupted')", name="operation_state"
    ),
    CheckConstraint("active_slot IS NULL OR active_slot = 1", name="operation_slot"),
    CheckConstraint(
        "owner_pid > 0 AND (base_revision IS NULL OR base_revision >= 0)", name="operation_owner"
    ),
)
Index("ix_operations_conversation", operations.c.conversation_id, operations.c.state)


conversation_turns = Table(
    "conversation_turns",
    metadata,
    Column("conversation_id", String, ForeignKey("conversations.id"), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("turn_id", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("prefix_sha256", String, nullable=False),
    CheckConstraint("position >= 0 AND json_valid(payload)", name="turn_position"),
    UniqueConstraint("conversation_id", "turn_id", name="uq_turn_identity"),
)


conversation_events = Table(
    "conversation_events",
    metadata,
    Column("conversation_id", String, ForeignKey("conversations.id"), primary_key=True),
    Column("kind", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("pending", Integer, nullable=False),
    CheckConstraint("pending IN (0,1) AND json_valid(payload)", name="event_pending"),
)
Index(
    "ix_conversation_events_current",
    conversation_events.c.conversation_id,
    conversation_events.c.pending,
)


conversation_bookmarks = Table(
    "conversation_bookmarks",
    metadata,
    Column("id", String, primary_key=True),
    Column("conversation_id", String, ForeignKey("conversations.id"), nullable=False),
    Column("payload", Text, nullable=False),
    CheckConstraint("json_valid(payload)", name="bookmark_json"),
)
Index("ix_bookmarks_conversation", conversation_bookmarks.c.conversation_id)


images = Table(
    "images",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
    Column(
        "state",
        String,
        Computed(
            "CASE WHEN json_valid(payload) THEN json_extract(payload, '$.state') END",
            persisted=True,
        ),
    ),
    CheckConstraint(
        "json_valid(payload) AND json_extract(payload, '$.run_id') = id", name="image_identity"
    ),
)


image_submissions = Table(
    "image_submissions",
    metadata,
    Column("id", String, primary_key=True),
    Column("fingerprint", String, nullable=False),
)


evaluations = Table(
    "evaluations",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
)


command_receipts = Table(
    "command_receipts",
    metadata,
    Column("id", String, primary_key=True),
    Column("fingerprint", String, nullable=False),
)


presets = Table(
    "presets",
    metadata,
    Column("id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("deleted", Boolean, nullable=False, default=False),
)
Index(
    "one_active_player",
    presets.c.kind,
    unique=True,
    sqlite_where=text("kind='player' AND deleted=0"),
)


preferences = Table(
    "preferences",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("payload", Text, nullable=False),
    CheckConstraint("id = 1 AND json_valid(payload)", name="preferences_singleton"),
)


creation_jobs = Table(
    "creation_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("owner_pid", Integer, nullable=False),
    Column("owner_started", String, nullable=False),
)


preset_saves = Table(
    "preset_saves",
    metadata,
    Column("id", String, primary_key=True),
    Column("fingerprint", String, nullable=False),
    Column("payload", Text, nullable=False),
)

objects = Table(
    "objects",
    metadata,
    Column("sha256", String(64), primary_key=True),
    Column("size_bytes", Integer, nullable=False),
    CheckConstraint(
        "length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*' AND size_bytes >= 0",
        name="object_digest",
    ),
)

artifact_bindings = Table(
    "artifact_bindings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("sha256", String(64), ForeignKey("objects.sha256"), nullable=False),
    Column("kind", String, nullable=False),
)
Index("ix_artifact_object", artifact_bindings.c.sha256)

llm_records = Table(
    "llm_records",
    metadata,
    Column("operation_id", String, primary_key=True),
    Column("purpose", String, primary_key=True),
    Column("attempt", Integer, primary_key=True),
    Column("revision", Integer, primary_key=True),
    Column("sha256", String(64), ForeignKey("objects.sha256"), nullable=False),
    Column("status", String, nullable=False),
    CheckConstraint("attempt >= 0 AND revision >= 0", name="llm_record_position"),
)

execution_budgets = Table(
    "execution_budgets",
    metadata,
    Column("id", String, primary_key=True),
    Column("max_calls", Integer, nullable=False),
    Column("max_seconds", Integer, nullable=False),
    Column("reserved_calls", Integer, nullable=False, server_default="0"),
    Column("expires_at", String, nullable=False),
    CheckConstraint(
        "max_calls > 0 AND reserved_calls BETWEEN 0 AND max_calls", name="budget_calls"
    ),
)

devices = Table(
    "devices",
    metadata,
    Column("id", String, primary_key=True),
    Column("token_sha256", String(64), nullable=False, unique=True),
    Column("label", String, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("expires_at", String, nullable=False),
    CheckConstraint("state IN ('pending','approved','revoked')", name="device_state"),
)

device_events = Table(
    "device_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("device_id", String, ForeignKey("devices.id"), nullable=False),
    Column("state", String, nullable=False),
    Column("recorded_at", String, nullable=False),
    CheckConstraint("state IN ('pending','approved','revoked')", name="device_event_state"),
)
Index("ix_device_events_device", device_events.c.device_id, device_events.c.recorded_at)
