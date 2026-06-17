"""PostgreSQL-backed test harness and relational fixture checks."""
import json

import pytest
from sqlalchemy import func, select

import security
from db import SessionLocal, engine
from schema import (
    AccommodationAllocation, Hostel, HostelRoom, Mess, MessAllocation,
    Participant,
)
from testing import factories
from testing.helpers import auth_headers, encrypt_for, make_qr, token_for


def test_database_engine_uses_postgresql():
    assert engine.dialect.name == "postgresql"


@pytest.fixture(scope="module")
def openapi_paths(app):
    """Generate OpenAPI and verify route/request models remain serializable."""
    return app.openapi()["paths"]


def test_app_exposes_the_whole_api(openapi_paths):
    paths = set(openapi_paths)
    for path in (
        "/profile/complete", "/auth/register", "/backend_teams",
        "/participants/statistics", "/workshops", "/workshop-slots",
        "/events", "/mess", "/hostels", "/audit-logs", "/embeddings",
        "/queries", "/issues",
    ):
        assert path in paths, f"{path} is not registered"


def test_route_count_is_stable(openapi_paths):
    operations = sum(len(methods) for methods in openapi_paths.values())
    assert operations >= 90, f"only {operations} operations registered"


def test_database_truncation_isolates_rows_between_tests():
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Participant)) == 0


def test_generators_are_reset_between_tests():
    from routers import backend_teams, events, hostels, workshops
    assert workshops.generator.current_id == 111
    assert hostels.generator.current_id == 111
    assert events.generator.current_event_id == 1111
    assert backend_teams.generator.current_id == 1111


def test_generators_are_reset_again_after_being_advanced():
    from routers import workshops
    workshops.generator.next_id()
    assert workshops.generator.current_id == 112


@pytest.mark.parametrize("path", ["/workshop-slots", "/workshops/public", "/events/public"])
def test_public_endpoints_need_no_token(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == []


def test_participant_factory_matches_registration_shape(make_participant):
    row = make_participant()
    for key in (
        "participant_id", "email", "password_hash", "profile", "mess",
        "accommodation", "photo", "qr_secrets", "embedding", "events",
        "workshops", "created_at", "updated_at",
    ):
        assert key in row
    assert set(row["mess"]) == {"registered", "mess_id", "scans", "payment"}
    assert set(row["accommodation"]) == {
        "registered", "hostel_id", "room", "arrival", "inside", "departure", "payment",
    }
    assert len(row["embedding"]["workshop"]) == 768


def test_participant_factory_deep_merges_overrides(make_participant):
    row = make_participant(profile={"house": "Gir"})
    assert row["profile"]["house"] == "Gir"
    assert row["profile"]["full_name"] == "Test Participant"
    assert row["profile"]["program"] == "DS"


def test_mess_and_hostel_allocations_use_relational_foreign_keys(password_hash):
    from datetime import datetime, timezone
    with SessionLocal.begin() as session:
        mess = Mess(mess_id="MESS1", name="North Hall", type="north_indian__veg", capacity=10)
        hostel = Hostel(hostel_id="HSTL111", name="Block A", gender="male", capacity=4, sharing=2)
        session.add_all([mess, hostel])
        session.flush()
        room = HostelRoom(hostel_id=hostel.id, room_number="101", capacity=2)
        participant = Participant(
            participant_id="DS23F000001", email="test@ds.study.iitm.ac.in",
            email_normalized="test@ds.study.iitm.ac.in", password_hash=password_hash,
            profile={}, mess_state={}, accommodation_state={}, qr_secrets={}, embedding={},
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
        )
        session.add_all([room, participant])
        session.flush()
        session.add(MessAllocation(participant_id=participant.id, mess_id=mess.id))
        session.add(AccommodationAllocation(participant_id=participant.id, room_id=room.id))
        session.flush()
        assert session.scalar(select(MessAllocation.mess_id).where(
            MessAllocation.participant_id == participant.id)) == mess.id
        assert session.scalar(select(AccommodationAllocation.room_id).where(
            AccommodationAllocation.participant_id == participant.id)) == room.id


def test_token_for_picks_the_namespace_from_the_document(participant, super_admin):
    from jose import jwt
    participant_claims = jwt.decode(token_for(participant), security.SECRET_KEY, algorithms=[security.ALGORITHM])
    staff_claims = jwt.decode(token_for(super_admin), security.SECRET_KEY, algorithms=[security.ALGORITHM])
    assert participant_claims["type"] == "participant"
    assert participant_claims["sub"] == participant["participant_id"]
    assert staff_claims["type"] == "staff"
    assert staff_claims["sub"] == super_admin["paradox_id"]


def test_real_token_passes_the_real_dependency_chain(client, participant):
    response = client.get("/workshops/my_registrations", headers=auth_headers(participant))
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.slow
def test_password_hash_is_real_bcrypt(password, password_hash):
    assert password_hash.startswith("$2b$")
    assert security.verify_password(password, password_hash)
    assert not security.verify_password("wrong-password", password_hash)


@pytest.mark.slow
def test_qr_payload_round_trips_through_real_rsa(participant, keypair):
    request = make_qr(participant, payload={"participant_id": participant["participant_id"]})
    decrypted = security.decrypt_qr_data(keypair[0], request["data"])
    assert decrypted == {"participant_id": participant["participant_id"]}


@pytest.mark.slow
def test_qr_payload_from_another_keypair_cannot_be_decrypted(keypair, alt_keypair):
    ciphertext = encrypt_for(alt_keypair[1], {"participant_id": "DS23F000001"})
    with pytest.raises(Exception):
        security.decrypt_qr_data(keypair[0], ciphertext)


def test_mess_menu_factory_writes_datetimes():
    from datetime import datetime
    menu = factories.mess_menu({1: ["breakfast", "lunch"]})
    slot = menu["day_1"]["breakfast"]
    assert isinstance(slot["start_time"], datetime)
    assert isinstance(slot["end_time"], datetime)
    assert slot["end_time"] > slot["start_time"]
    assert set(menu["day_1"]) == {"breakfast", "lunch"}


def test_announcement_factory_uses_datetime_not_string():
    from datetime import datetime
    assert isinstance(factories.announcement()["created_at"], datetime)


def test_audit_row_factory_matches_logger_shape():
    row = factories.mess_scan_row(day=2, slot="lunch")
    assert set(row) == {
        "timestamp", "actor_id", "actor_name", "actor_type", "actor_role",
        "action", "target_id", "details",
    }
    assert row["action"] == "MESS_SCAN"
    assert row["details"] == {"participant_id": "DS23F000001", "slot": "lunch", "day": 2}
    json.dumps({key: value for key, value in row.items() if key != "timestamp"})
