"""Manage visibility without editing the recorded conversation or generation."""

from uuid import UUID

from sqlalchemy import Connection, and_, case, func, select, true
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.sql.elements import ColumnElement

from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.errors import conflict, missing
from komaquill.adapters.persistence.schema import (
    archived_conversations,
    command_receipts,
    conversation_bookmarks,
    conversation_turns,
    conversations,
    creation_jobs,
    image_detachments,
    images,
    operations,
    presets,
    work_metadata,
)
from komaquill.adapters.storage_errors import stored
from komaquill.domain.errors import KomaquillError
from komaquill.domain.library.works import (
    ImageDetach,
    ImageUse,
    WorkChange,
    WorkItem,
    WorkKind,
    WorkRef,
)
from komaquill.domain.models import TERMINAL_RUN_STATES, Run
from komaquill.domain.workflow import digest


def work_key(ref: WorkRef) -> ColumnElement[bool]:
    return and_(
        work_metadata.c.kind == ref.kind,
        work_metadata.c.id == str(ref.id),
        work_metadata.c.output_index == ref.index,
    )


def visible_work(
    kind: WorkKind, identity: str | ColumnElement[str], index: int = 0
) -> ColumnElement[bool]:
    return (
        ~select(work_metadata.c.id)
        .where(
            work_metadata.c.kind == kind,
            work_metadata.c.id == identity,
            work_metadata.c.output_index == index,
            work_metadata.c.trashed.is_(True),
        )
        .correlate_except(work_metadata)
        .exists()
    )


def detached_image(connection: Connection, use: ImageUse, run: UUID, index: int) -> bool:
    return (
        connection.execute(
            select(image_detachments.c.run_id).where(
                image_detachments.c.owner_kind == use.kind,
                image_detachments.c.owner_id == str(use.owner),
                image_detachments.c.slot == use.slot,
                image_detachments.c.run_id == str(run),
                image_detachments.c.output_index == index,
            )
        ).scalar_one_or_none()
        is not None
    )


class WorkRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def _item(self, connection: Connection, ref: WorkRef) -> WorkItem:
        identity = str(ref.id)
        row = (
            connection.execute(select(work_metadata).where(work_key(ref))).mappings().one_or_none()
        )
        owner = None
        if ref.kind == "image":
            payload = connection.execute(
                select(images.c.payload).where(images.c.id == identity)
            ).scalar_one_or_none()
            if payload is None:
                raise missing()
            run = Run.model_validate_json(payload)
            if ref.index >= len(run.images):
                raise missing()
            name = run.images[ref.index]
        elif ref.kind == "bookmark":
            saved = connection.execute(
                select(
                    func.json_extract(conversation_bookmarks.c.payload, "$.name"),
                    conversation_bookmarks.c.conversation_id,
                ).where(conversation_bookmarks.c.id == identity)
            ).one_or_none()
            if saved is None:
                raise missing()
            name, owner = saved
        else:
            if ref.kind in {"story", "character"}:
                statement = select(func.json_extract(presets.c.payload, "$.name")).where(
                    presets.c.id == identity,
                    presets.c.kind == ref.kind,
                    true() if row is not None and row["trashed"] else presets.c.deleted.is_(False),
                )
            else:
                table = archived_conversations if ref.kind == "archived" else conversations
                statement = select(
                    func.coalesce(
                        case(
                            (
                                func.json_valid(table.c.payload),
                                func.json_extract(table.c.payload, "$.definition.title"),
                            )
                        ),
                        table.c.id,
                    )
                ).where(table.c.id == identity)
                if ref.kind == "archived":
                    statement = statement.where(archived_conversations.c.reason != "converted")
            name = connection.execute(statement).scalar_one_or_none()
            if name is None:
                raise missing()
        return WorkItem.model_validate(
            {
                **ref.model_dump(),
                "name": (row["name"] if row else None) or name,
                "revision": row["revision"] if row else 0,
                "trashed": bool(row["trashed"]) if row else False,
                "owner": owner,
            }
        )

    @stored
    def get(self, ref: WorkRef) -> WorkItem:
        with self.database.engine.connect() as connection:
            item = self._item(connection, ref)
            if item.trashed or (
                item.owner
                and not connection.execute(
                    select(conversations.c.id).where(
                        conversations.c.id == str(item.owner),
                        visible_work("conversation", conversations.c.id),
                    )
                ).first()
            ):
                raise missing()
            return item

    @stored
    def trash(self) -> list[WorkItem]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(work_metadata.c.kind, work_metadata.c.id, work_metadata.c.output_index)
                .where(work_metadata.c.trashed.is_(True))
                .order_by(work_metadata.c.updated_at.desc(), work_metadata.c.id)
            )
            return [
                self._item(
                    connection,
                    WorkRef.model_validate({"kind": kind, "id": identity, "index": index}),
                )
                for kind, identity, index in rows
            ]

    @stored
    def image_outputs(self, runs: list[Run]) -> dict[UUID, list[WorkItem]]:
        if not runs:
            return {}
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(work_metadata).where(
                    work_metadata.c.kind == "image",
                    work_metadata.c.id.in_([str(run.run_id) for run in runs]),
                )
            ).mappings()
            overrides = {(row["id"], row["output_index"]): row for row in rows}
        outputs = {}
        for run in runs:
            outputs[run.run_id] = [
                WorkItem(
                    kind="image",
                    id=run.run_id,
                    index=index,
                    name=(metadata["name"] if metadata else None) or filename,
                    revision=metadata["revision"] if metadata else 0,
                )
                for index, filename in enumerate(run.images)
                if not (metadata := overrides.get((str(run.run_id), index)))
                or not metadata["trashed"]
            ]
        return outputs

    def _uses(self, connection: Connection, ref: WorkRef) -> list[ImageUse]:
        identity = str(ref.id)
        uses = []
        if ref.index == 0:
            for row in connection.execute(
                select(
                    presets.c.kind, presets.c.id, func.json_extract(presets.c.payload, "$.name")
                ).where(
                    presets.c.kind.in_(["character", "story"]),
                    func.json_extract(presets.c.payload, "$.image_id") == identity,
                    presets.c.deleted.is_(False)
                    | select(work_metadata.c.id)
                    .where(
                        work_metadata.c.kind == presets.c.kind,
                        work_metadata.c.id == presets.c.id,
                        work_metadata.c.trashed.is_(True),
                    )
                    .exists(),
                )
            ):
                uses.append(ImageUse(kind=row[0], owner=row[1], slot="cover", name=row[2]))
            for kind in ("character", "story"):
                path = f"$.definition.presets.{kind}.image_id"
                for owner, name in connection.execute(
                    select(
                        conversations.c.id,
                        func.json_extract(conversations.c.payload, "$.definition.title"),
                    ).where(func.json_extract(conversations.c.payload, path) == identity)
                ):
                    uses.append(
                        ImageUse(
                            kind="conversation", owner=UUID(str(owner)), slot=kind, name=str(name)
                        )
                    )
        for owner, turn, name in connection.execute(
            select(
                conversation_turns.c.conversation_id,
                conversation_turns.c.turn_id,
                func.json_extract(conversations.c.payload, "$.definition.title"),
            )
            .join(conversations)
            .where(func.json_extract(conversation_turns.c.payload, "$.image_id") == identity)
        ):
            uses.append(
                ImageUse(
                    kind="conversation", owner=UUID(str(owner)), slot=f"turn:{turn}", name=str(name)
                )
            )
        for owner, job, name in connection.execute(
            select(
                conversations.c.id,
                creation_jobs.c.id,
                func.json_extract(conversations.c.payload, "$.definition.title"),
            )
            .join(
                conversations,
                conversations.c.id
                == func.json_extract(creation_jobs.c.payload, "$.request.target_id"),
            )
            .where(
                func.json_extract(creation_jobs.c.payload, "$.request.kind")
                == "conversation_image",
                func.json_extract(creation_jobs.c.payload, "$.image_id") == identity,
            )
        ):
            uses.append(
                ImageUse(
                    kind="conversation", owner=UUID(str(owner)), slot=f"job:{job}", name=str(name)
                )
            )
        return [use for use in uses if not detached_image(connection, use, ref.id, ref.index)]

    @stored
    def image_uses(self, ref: WorkRef) -> list[ImageUse]:
        if ref.kind != "image":
            raise missing()
        with self.database.engine.connect() as connection:
            self._item(connection, ref)
            return self._uses(connection, ref)

    @stored
    def detach(self, request: ImageDetach) -> None:
        with self.database.write() as connection:
            ref, use = request.image, request.use
            if ref.kind != "image" or self._item(connection, ref).trashed:
                raise missing()
            if detached_image(connection, use, ref.id, ref.index):
                return
            if not any(
                (item.kind, item.owner, item.slot) == (use.kind, use.owner, use.slot)
                for item in self._uses(connection, ref)
            ):
                raise conflict()
            if use.kind in {"character", "story"}:
                if self._busy(connection, WorkRef(kind=use.kind, id=use.owner)):
                    raise KomaquillError(code="operation_busy", retryable=True)
                connection.execute(
                    presets.update()
                    .where(presets.c.id == str(use.owner))
                    .values(
                        payload=func.json_set(
                            presets.c.payload,
                            "$.image_id",
                            None,
                            "$.image_job_id",
                            None,
                            "$.image_stale",
                            func.json("false"),
                            "$.revision",
                            func.json_extract(presets.c.payload, "$.revision") + 1,
                        ),
                    )
                )
                return
            connection.execute(
                image_detachments.insert().values(
                    owner_kind=use.kind,
                    owner_id=str(use.owner),
                    slot=use.slot,
                    run_id=str(ref.id),
                    output_index=ref.index,
                )
            )

    @stored
    def detached(self, kind: str, owner: UUID) -> dict[str, list[int]]:
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(
                    image_detachments.c.slot,
                    image_detachments.c.run_id,
                    image_detachments.c.output_index,
                ).where(
                    image_detachments.c.owner_kind == kind,
                    image_detachments.c.owner_id == str(owner),
                )
            )
            result: dict[str, list[int]] = {}
            for slot, run, index in rows:
                result.setdefault(f"{slot}:{run}", []).append(int(str(index)))
            return result

    def _busy(self, connection: Connection, ref: WorkRef) -> bool:
        identity = str(ref.id)
        if (
            ref.kind == "conversation"
            and connection.execute(
                select(operations.c.id).where(
                    operations.c.conversation_id == identity, operations.c.active_slot == 1
                )
            ).first()
        ):
            return True
        if (
            ref.kind == "image"
            and connection.execute(
                select(images.c.id).where(
                    images.c.id == identity, images.c.state.not_in(TERMINAL_RUN_STATES)
                )
            ).first()
        ):
            return True
        if ref.kind in {"conversation", "character", "story", "image"}:
            column = "$.image_id" if ref.kind == "image" else "$.request.target_id"
            state = func.json_extract(creation_jobs.c.payload, "$.state")
            terminal = (
                select(images.c.id)
                .where(images.c.id == creation_jobs.c.id, images.c.state.in_(TERMINAL_RUN_STATES))
                .exists()
            )
            return (
                connection.execute(
                    select(creation_jobs.c.id).where(
                        func.json_extract(creation_jobs.c.payload, column) == identity,
                        state.in_(["accepted", "running"]) | ((state == "submitted") & ~terminal),
                    )
                ).first()
                is not None
            )
        return False

    @stored
    def change(self, request: WorkChange) -> WorkItem:
        ref = request.target
        receipt = str(request.operation_id)
        fingerprint = digest(request.model_dump(mode="json"))
        with self.database.write() as connection:
            item = self._item(connection, ref)
            previous = connection.execute(
                select(command_receipts.c.fingerprint).where(command_receipts.c.id == receipt)
            ).scalar_one_or_none()
            if previous is not None:
                if previous != fingerprint:
                    raise conflict()
                return item
            if item.revision != request.expected_revision or item.trashed != (
                request.action == "restore"
            ):
                raise conflict()
            if request.expected_content_revision is not None and ref.kind in {"character", "story"}:
                revision: int = connection.execute(
                    select(func.json_extract(presets.c.payload, "$.revision")).where(
                        presets.c.id == str(ref.id)
                    )
                ).scalar_one()
                if revision != request.expected_content_revision:
                    raise conflict()
            if request.action == "trash":
                if self._busy(connection, ref):
                    raise KomaquillError(code="operation_busy", retryable=True)
                if ref.kind == "image" and self._uses(connection, ref):
                    raise KomaquillError(code="image_in_use", retryable=False)
            trashed = request.action == "trash"
            name = request.name.strip() if request.name else item.name
            changes = dict(
                name=None if ref.kind in {"character", "story", "bookmark"} else name,
                revision=item.revision + 1,
                trashed=trashed,
                updated_at=func.current_timestamp(),
            )
            statement = insert(work_metadata).values(
                kind=ref.kind,
                id=str(ref.id),
                output_index=ref.index,
                **changes,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        work_metadata.c.kind,
                        work_metadata.c.id,
                        work_metadata.c.output_index,
                    ],
                    set_=changes,
                )
            )
            if ref.kind == "bookmark" and request.action == "rename":
                connection.execute(
                    conversation_bookmarks.update()
                    .where(conversation_bookmarks.c.id == str(ref.id))
                    .values(payload=func.json_set(conversation_bookmarks.c.payload, "$.name", name))
                )
            if ref.kind in {"character", "story"}:
                if request.action == "rename":
                    raise KomaquillError(code="invalid_request", retryable=False)
                connection.execute(
                    presets.update().where(presets.c.id == str(ref.id)).values(deleted=trashed)
                )
            connection.execute(
                command_receipts.insert().values(id=receipt, fingerprint=fingerprint)
            )
            return item.model_copy(
                update={"name": name, "revision": item.revision + 1, "trashed": trashed}
            )
