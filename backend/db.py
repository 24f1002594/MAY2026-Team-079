"""PostgreSQL engine and session configuration."""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker


load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://localhost/paradox")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]
if not DATABASE_URL.startswith("postgresql+psycopg://"):
    raise RuntimeError("DATABASE_URL must use PostgreSQL with the psycopg driver")

_url_for_logs = re.sub(r"(://[^:/@]+):[^/@]+@", r"\1:***@", DATABASE_URL)
engine: Engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=int(os.getenv("DATABASE_POOL_RECYCLE_SECONDS", "1800")),
    connect_args={
        "connect_timeout": int(os.getenv("DATABASE_CONNECT_TIMEOUT_SECONDS", "5")),
        "options": "-c timezone=UTC",
    },
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def check_connection() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def create_schema() -> None:
    """Create the schema for local bootstrap; deployed environments use migrations."""
    from schema import Base

    Base.metadata.create_all(engine)

