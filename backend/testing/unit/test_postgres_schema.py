"""Relational integrity guarantees for the PostgreSQL schema."""
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from db import SessionLocal
from schema import (
    Base,
    Participant,
    Workshop,
    WorkshopRegistration,
    WorkshopSlot,
)


def _participant(suffix: str) -> Participant:
    return Participant(
        participant_id=f"DS{suffix}",
        email=f"{suffix}@example.test",
        email_normalized=f"{suffix}@example.test",
        password_hash="test-hash",
    )


def test_domain_tables_have_relational_primary_keys():
    expected = {
        "participants", "staff_users", "events", "event_registrations",
        "workshops", "workshop_slots", "workshop_registrations",
        "messes", "mess_allocations", "hostels", "hostel_rooms",
        "accommodation_allocations", "facility_issues", "support_queries",
        "audit_logs", "system_logs",
    }
    assert expected <= set(Base.metadata.tables)
    for table_name in expected:
        assert Base.metadata.tables[table_name].primary_key.columns


def test_normalized_participant_email_is_unique():
    with SessionLocal() as session:
        session.add(_participant("unique-one"))
        session.commit()
        session.add(_participant("unique-two"))
        session.flush()
        duplicate = _participant("unique-three")
        duplicate.email = "UNIQUE-ONE@example.test"
        duplicate.email_normalized = "unique-one@example.test"
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_participant_cannot_hold_two_active_bookings_for_one_slot():
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        participant = _participant("slot-booking")
        slot = WorkshopSlot(
            slot_id="TEST-SLOT",
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=2),
        )
        session.add_all([participant, slot])
        session.flush()
        first = Workshop(
            workshop_id="TEST-WKSP-1", name="First", slot_id=slot.id,
            start_time=slot.start_time, capacity=10,
        )
        second = Workshop(
            workshop_id="TEST-WKSP-2", name="Second", slot_id=slot.id,
            start_time=slot.start_time, capacity=10,
        )
        session.add_all([first, second])
        session.flush()
        session.add(WorkshopRegistration(
            participant_id=participant.id, workshop_id=first.id, slot_id=slot.id,
        ))
        session.commit()
        session.add(WorkshopRegistration(
            participant_id=participant.id, workshop_id=second.id, slot_id=slot.id,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_database_foreign_key_cascades_registration_on_participant_delete():
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        participant = _participant("cascade")
        slot = WorkshopSlot(
            slot_id="CASCADE-SLOT",
            start_time=now + timedelta(hours=1),
            end_time=now + timedelta(hours=2),
        )
        session.add_all([participant, slot])
        session.flush()
        workshop = Workshop(
            workshop_id="CASCADE-WKSP", name="Cascade", slot_id=slot.id,
            start_time=slot.start_time, capacity=10,
        )
        session.add(workshop)
        session.flush()
        session.add(WorkshopRegistration(
            participant_id=participant.id, workshop_id=workshop.id, slot_id=slot.id,
        ))
        participant_id = participant.id
        session.commit()
        session.execute(delete(Participant).where(Participant.id == participant_id))
        session.commit()
        remaining = session.scalar(
            select(func.count()).select_from(WorkshopRegistration).where(
                WorkshopRegistration.participant_id == participant_id
            )
        )
        assert remaining == 0
