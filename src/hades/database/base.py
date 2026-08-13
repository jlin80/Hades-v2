"""Declarative base and metadata conventions for all ORM models.

A naming convention is fixed here from the very first migration. Without one,
PostgreSQL auto-generates constraint names, Alembic autogenerate cannot reliably
detect or drop them, and the unique constraints that Phase 2 will rely on for
snapshot idempotency (task.md §13) become awkward to reference by name.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for every Hades ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
