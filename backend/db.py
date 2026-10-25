from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    """Base class for all persisted domain models."""


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            # Connectivity alone is insufficient: an old schema may accept
            # SELECT 1 while rounding saves and timeline reads fail with 500.
            inspector = inspect(connection)
            for table in Base.metadata.tables.values():
                if not inspector.has_table(table.name):
                    return False
                actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
                if not set(table.columns.keys()).issubset(actual_columns):
                    return False
        return True
    except Exception:
        return False
