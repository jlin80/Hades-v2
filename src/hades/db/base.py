"""Declarative base shared by every table."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base for all ORM models. Alembic autogenerates against its metadata."""
