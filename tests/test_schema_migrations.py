"""Reproduce an existing database; create_all-based tests miss schema drift."""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

from backend import db, models


def test_existing_rounding_schema_upgrades_without_losing_records(monkeypatch):
    engine = create_engine("sqlite://")
    db.Base.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    assert db.check_database()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE rounding_sessions DROP COLUMN audio_filename"))
        connection.execute(text("ALTER TABLE rounding_sessions DROP COLUMN transcript_source"))
        connection.execute(text("INSERT INTO rounding_sessions (id, encounter_id, doctor_id, started_at, transcript, status) VALUES (1, 1, 1, '2026-10-23', 'existing synthetic rounding', 'DRAFT')"))
    assert not db.check_database()

    migration = importlib.import_module("backend.alembic.versions.0003_rounding_transcript_columns")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()  # Safe when columns already exist.
        saved = connection.execute(text("SELECT transcript, audio_filename, transcript_source FROM rounding_sessions WHERE id=1")).one()
        assert saved == ("existing synthetic rounding", None, "manual")
        columns = {c["name"] for c in inspect(connection).get_columns("rounding_sessions")}
        assert {"audio_filename", "transcript_source"}.issubset(columns)
    assert db.check_database()
    engine.dispose()
