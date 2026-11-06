"""Retain revocable device approvals while storing only token hashes."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from komaquill.adapters.persistence.connection import Database
from komaquill.adapters.persistence.schema import device_events, devices
from komaquill.adapters.storage_errors import stored
from komaquill.adapters.web_configuration import WebSettings
from komaquill.domain.errors import KomaquillError


class DeviceStore:
    def __init__(self, database: Database, settings: WebSettings) -> None:
        self.database, self.settings = database, settings

    @stored
    def enroll(self) -> tuple[str, str]:
        now = datetime.now(UTC)
        token, identity = secrets.token_urlsafe(32), str(uuid4())
        with self.database.transaction() as connection:
            count = connection.execute(
                select(func.count())
                .select_from(devices)
                .where(devices.c.state == "pending", devices.c.expires_at > now.isoformat())
            ).scalar_one()
            if count >= self.settings.pending_limit:
                raise KomaquillError(code="operation_busy")
            connection.execute(
                devices.insert().values(
                    id=identity,
                    token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                    label=identity[:8],
                    state="pending",
                    created_at=now.isoformat(),
                    expires_at=(now + timedelta(minutes=10)).isoformat(),
                )
            )
            connection.execute(
                device_events.insert().values(
                    id=str(uuid4()),
                    device_id=identity,
                    state="pending",
                    recorded_at=now.isoformat(),
                )
            )
            connection.commit()
        return identity, token

    @stored
    def state(self, token: str | None) -> str | None:
        if token is None or len(token) > 128:
            return None
        with self.database.engine.connect() as connection:
            return connection.execute(
                select(devices.c.state).where(
                    devices.c.token_sha256 == hashlib.sha256(token.encode()).hexdigest(),
                    devices.c.expires_at > datetime.now(UTC).isoformat(),
                )
            ).scalar_one_or_none()

    @stored
    def list(self) -> list[dict[str, str]]:
        with self.database.engine.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    select(
                        devices.c.id,
                        devices.c.label,
                        devices.c.state,
                        devices.c.created_at,
                        devices.c.expires_at,
                    )
                ).mappings()
            ]

    @stored
    def decide(self, identity: UUID, *, approve: bool) -> None:
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            row = (
                connection.execute(select(devices).where(devices.c.id == str(identity)))
                .mappings()
                .first()
            )
            if row is None or row["expires_at"] <= now.isoformat():
                raise KomaquillError(code="request_not_found")
            if approve and row["state"] != "pending":
                raise KomaquillError(code="operation_conflict")
            connection.execute(
                devices.update()
                .where(devices.c.id == str(identity))
                .values(
                    state="approved" if approve else "revoked",
                    expires_at=(
                        now + timedelta(days=self.settings.device_lifetime_days)
                    ).isoformat()
                    if approve
                    else now.isoformat(),
                )
            )
            connection.execute(
                device_events.insert().values(
                    id=str(uuid4()),
                    device_id=str(identity),
                    state="approved" if approve else "revoked",
                    recorded_at=now.isoformat(),
                )
            )
            connection.commit()
