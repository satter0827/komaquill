"""Verify the immutable prior response before accepting an appended validation."""

import json
from uuid import uuid4

import pytest
from komaquill.adapters.persistence.artifacts import ArtifactRepository
from komaquill.adapters.persistence.connection import database
from komaquill.adapters.persistence.schema import llm_records, objects
from komaquill.api import Client, KomaquillError
from komaquill.domain.artifacts import ObjectRef
from sqlalchemy import func, select


def test_appending_a_validation_rejects_a_corrupted_prior_response(tmp_path):
    api = Client.initialize(tmp_path)
    repository = ArtifactRepository(database(tmp_path / ".artifacts/data/komaquill.sqlite3"))
    operation = uuid4()
    payload = {"status": "response_received", "response": "original", "usage": {}}
    repository.record(operation, "reply", 0, payload)
    with repository.database.engine.connect() as connection:
        row = connection.execute(select(objects)).mappings().one()
    reference = ObjectRef.model_validate(dict(row))
    corrupted = json.dumps(dict(payload, observation="changed without a matching hash")).encode()
    repository.objects.path(reference).write_bytes(corrupted)
    with pytest.raises(KomaquillError, match="identity_mismatch"):
        repository.record(operation, "reply", 0, dict(payload, validation="checked"))
    with repository.database.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(llm_records)).scalar_one() == 1
    assert repository.objects.path(reference).read_bytes() == corrupted
    api.close()
