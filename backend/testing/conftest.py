"""PostgreSQL-backed backend test harness."""
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from uuid import UUID

BACKEND_ROOT = Path(__file__).resolve().parent.parent
os.environ["TESTING"] = "1"
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    raise RuntimeError("Set TEST_DATABASE_URL to a dedicated PostgreSQL test database before running backend tests")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import select, text
from db import SessionLocal, engine
from schema import Base, Participant, StaffUser, SystemLog
from account_repository import participant_record, staff_record

import log_config
import log_context
import main as main_module
import security
from routers import backend_teams as backend_teams_router
from routers import embeddings as embeddings_router
from routers import events as events_router
from routers import hostels as hostels_router
from routers import workshops as workshops_router

from testing import factories
from testing.helpers import auth_headers

# The one password every seeded account shares. Hashed once per session with
# real bcrypt at full cost (see `password_hash`), because requirement 2 of the
# plan is that tokens and hashes are genuine — only the recomputation is
# avoided, never the algorithm.
PASSWORD = "correct-horse-battery"


# ---------------------------------------------------------------------------
# Session-scoped cryptographic material
#
# A bcrypt hash costs ~250ms and an RSA-2048 keypair ~100-500ms. Generating
# either per test would put the suite in the multi-minute range for no gain in
# fidelity: the hash a test verifies against and the key a QR payload is
# encrypted with are real either way. So they are generated once and the same
# real values are re-inserted by each test's fixtures.
#
# Tests that genuinely need a *second*, different keypair (proving a QR
# encrypted for one participant cannot be decrypted for another) take
# `alt_keypair`, which is a distinct real keypair.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def password() -> str:
    return PASSWORD


@pytest.fixture(scope="session")
def password_hash() -> str:
    return security.get_password_hash(PASSWORD)


@pytest.fixture(scope="session")
def keypair() -> tuple:
    """(private_pem, public_pem) — a real 2048-bit RSA keypair."""
    return security.generate_rsa_key_pair()


@pytest.fixture(scope="session")
def alt_keypair() -> tuple:
    """A second, unrelated real keypair, for cross-key rejection tests."""
    return security.generate_rsa_key_pair()


# ---------------------------------------------------------------------------
# Per-test isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_database():
    """Create the mapped schema once and truncate it between tests."""
    Base.metadata.create_all(engine)
    table_names = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def reset_process_state():
    """
    Reset the four module-level ID generators and the embeddings rate limiter.

    All five are process-global and survive between tests, which makes any
    assertion on a generated id (``WKSP111``, ``HSTL111``, ``EVTEC1111``,
    ``SAWO1111``) order-dependent, and makes the second ``/embeddings`` test in
    a session fail with a 429 it never asked for.
    """
    workshops_router.generator.current_id = 111
    hostels_router.generator.current_id = 111
    events_router.generator.current_event_id = 1111
    events_router.generator.current_round_id = 11111
    events_router.generator.current_team_id = 111111
    backend_teams_router.generator.current_id = 1111
    embeddings_router._last_request_at.clear()
    # `RequestLogMiddleware` binds the correlation id and never resets it, by
    # design, so one test's request id would otherwise be stamped onto the audit
    # rows written by the next test that does not go through HTTP.
    log_context.clear()
    yield
    embeddings_router._last_request_at.clear()
    log_context.clear()


# ---------------------------------------------------------------------------
# App / client
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def app():
    return main_module.app


@pytest.fixture()
def restore_logging():
    """
    Put the logging configuration back after a client has been closed.

    The app's `lifespan` shutdown hook calls `logging.shutdown()`, which flushes
    and closes **every** handler in the process — including the ones pytest's
    `caplog` installs. Without this, a test that opens a client and a later test
    that asserts on log records would interact through a global side effect.
    """
    yield
    log_config._configured = False
    log_config.configure_logging(force=True)


@pytest.fixture()
def client(app, restore_logging):
    # `raise_server_exceptions=False` so a route that genuinely 500s (several
    # xfail-marked defects do) surfaces as a 500 response the test can assert
    # on, rather than as an exception escaping the client.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture()
def strict_client(app, restore_logging):
    """A client that re-raises server exceptions — useful when a test wants the
    traceback rather than a 500 status."""
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Audit-trail helpers
#
# Almost every route now writes at least one `system_logs` row, and several write
# more than one (a refusal row plus a batch summary, say). Tests therefore assert
# on the presence and content of a *named* action rather than on a row count.
# ---------------------------------------------------------------------------

@pytest.fixture()
def audit():
    class AuditTrail:
        @staticmethod
        def rows(action=None):
            with SessionLocal() as session:
                query = select(SystemLog).order_by(SystemLog.created_at, SystemLog.id)
                if action:
                    query = query.where(SystemLog.action == action)
                return [{
                    "timestamp": row.created_at,
                    "actor_id": row.actor_id,
                    "actor_name": row.actor_name,
                    "actor_type": row.actor_type,
                    "actor_role": row.actor_role,
                    "action": row.action,
                    "target_id": row.target_id,
                    "details": row.details or {},
                } for row in session.scalars(query).all()]

        @staticmethod
        def actions():
            with SessionLocal() as session:
                return list(session.scalars(select(SystemLog.action).order_by(SystemLog.created_at, SystemLog.id)).all())

        def one(self, action):
            found = self.rows(action)
            assert len(found) == 1, f"expected exactly one {action} row, found {len(found)}"
            return found[0]

        def latest(self, action):
            found = self.rows(action)
            assert found, f"no {action} row was written; actions present: {self.actions()}"
            return found[-1]

        def none(self, action):
            assert not self.rows(action), f"unexpected {action} row"

    return AuditTrail()


# ---------------------------------------------------------------------------
# Document fixtures
#
# Every one of these inserts a real document and returns it (with its `_id`),
# so a test can mint a real token for it via `auth_headers`.
# ---------------------------------------------------------------------------

@pytest.fixture()
def make_participant(password_hash, keypair):
    """
    Factory for inserted participants.

    ``make_participant(email=..., profile={...})`` — overrides are deep-merged
    onto the document shape ``POST /auth/register`` actually creates, so a test
    that cares about one profile field does not have to restate the rest.
    """
    created = []

    def _make(**overrides):
        doc = factories.participant_doc(
            password_hash=password_hash,
            private_key=keypair[0],
            public_key=keypair[1],
            **overrides,
        )
        profile = dict(doc.get("profile") or {})
        dob = profile.get("dob")
        from datetime import date
        try:
            date_of_birth = date.fromisoformat(dob) if dob else None
        except (TypeError, ValueError):
            date_of_birth = None
        def db_timestamp(value):
            if value is None:
                return None
            if isinstance(value, str):
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    pass
            if not isinstance(value, datetime):
                return value
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        row = Participant(
            participant_id=doc["participant_id"], email=doc["email"],
            email_normalized=doc["email"].strip().lower(), password_hash=doc["password_hash"],
            full_name=profile.get("full_name"), date_of_birth=date_of_birth,
            house=profile.get("house"), gender=profile.get("gender"), phone=profile.get("phone"),
            country=profile.get("country"), state=profile.get("state"), city=profile.get("city"),
            address=profile.get("address"), program=profile.get("program"),
            course_stage=profile.get("course_stage"), mess_preference=profile.get("mess_preference"),
            profile=profile, photo=doc.get("photo"), qr_secrets=doc.get("qr_secrets") or {},
            embedding=doc.get("embedding") or {}, mess_state=doc.get("mess") or {},
            accommodation_state=doc.get("accommodation") or {},
            attributes={"events": doc.get("events", []), "workshops": doc.get("workshops", [])},
            created_at=db_timestamp(doc.get("created_at")), updated_at=db_timestamp(doc.get("updated_at")),
        )
        with SessionLocal.begin() as session:
            session.add(row)
            session.flush()
            stored = participant_record(session, row)
        created.append(stored)
        return stored

    return _make


@pytest.fixture()
def make_staff(password_hash):
    """Factory for inserted backend_teams accounts."""
    def _make(**overrides):
        doc = factories.staff_doc(password_hash=password_hash, **overrides)
        admin_id = doc.get("admin_id")
        row = StaffUser(
            paradox_id=doc["paradox_id"], email=doc["email"],
            email_normalized=doc["email"].strip().lower(), password_hash=doc["password_hash"],
            role=doc["role"], department=doc.get("department"),
            participant_id=UUID(str(admin_id)) if admin_id else None,
            attributes={"name": doc.get("name"), "designation": doc.get("designation")},
            created_at=(doc["created_at"].replace(tzinfo=timezone.utc) if doc.get("created_at") and doc["created_at"].tzinfo is None else doc.get("created_at")),
            updated_at=(doc["updated_at"].replace(tzinfo=timezone.utc) if doc.get("updated_at") and doc["updated_at"].tzinfo is None else doc.get("updated_at")),
        )
        with SessionLocal.begin() as session:
            session.add(row)
            session.flush()
            return staff_record(row)

    return _make


@pytest.fixture()
def participant(make_participant):
    """Return the default account, reusing it when a parent fixture seeded it."""
    from sqlalchemy import select

    with SessionLocal() as session:
        row = session.scalar(select(Participant).where(
            Participant.participant_id == "DS23F000001"
        ))
        if row:
            return participant_record(session, row)
    return make_participant()


@pytest.fixture()
def other_participant(make_participant):
    return make_participant(
        participant_id="DS23F000002",
        email="23f000002@ds.study.iitm.ac.in",
        profile={"full_name": "Second Participant", "gender": "female", "house": "Gir"},
    )


@pytest.fixture()
def super_admin(make_staff):
    return make_staff(paradox_id="SAWO1111", role="super_admin", department="workshops")


@pytest.fixture()
def plain_staff(make_staff):
    """A valid staff account with no elevated rights anywhere."""
    return make_staff(
        paradox_id="ADTE2222",
        email="dept.admin@ds.study.iitm.ac.in",
        role="admin",
        department="technical",
    )


@pytest.fixture()
def other_role_staff(make_staff):
    """Role ``other`` — the only role a hostel team member may hold."""
    return make_staff(
        paradox_id="OTHO3333",
        email="block.desk@ds.study.iitm.ac.in",
        role="other",
        department="hostels",
    )


@pytest.fixture()
def participant_headers(participant):
    return auth_headers(participant)


@pytest.fixture()
def admin_headers(super_admin):
    return auth_headers(super_admin)


@pytest.fixture()
def staff_headers(plain_staff):
    return auth_headers(plain_staff)
