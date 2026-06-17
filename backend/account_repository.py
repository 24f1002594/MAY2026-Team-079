"""Account queries and API projections over normalized PostgreSQL tables."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from db import SessionLocal
from schema import (
    AccommodationAllocation, Event, EventRegistration, Hostel, HostelRoom, Mess,
    MessAllocation, MessAttendance, Participant, Payment, StaffUser, Workshop,
    WorkshopAttendance, WorkshopRegistration,
)


def participant_record(session, participant: Participant) -> dict[str, Any]:
    """Return the established account shape while registrations stay relational."""
    data = {
        "_id": str(participant.id),
        "participant_id": participant.participant_id,
        "email": participant.email,
        "password_hash": participant.password_hash,
        "role": participant.role,
        "profile": participant.profile or {},
        "mess": dict(participant.mess_state or {}),
        "accommodation": dict(participant.accommodation_state or {}),
        "photo": participant.photo,
        "qr_secrets": participant.qr_secrets or {},
        "embedding": participant.embedding or {},
        "attributes": participant.attributes or {},
        "created_at": participant.created_at,
        "updated_at": participant.updated_at,
    }
    mess_allocation = session.execute(
        select(MessAllocation)
        .join(Mess, MessAllocation.mess_id == Mess.id)
        .where(MessAllocation.participant_id == participant.id)
    ).first()
    if mess_allocation:
        allocation = mess_allocation[0]
        # The established participant context uses the mess row's UUID here;
        # public API responses translate it back to `mess_id` in _mess_record.
        data["mess"]["mess_id"] = str(allocation.mess_id)
        data["mess"]["registered"] = allocation.status == "allocated"
    meal_scans = session.scalars(
        select(MessAttendance)
        .where(MessAttendance.participant_id == participant.id)
        .order_by(MessAttendance.scanned_at)
    ).all()
    if meal_scans:
        scans: dict[str, dict] = {}
        for scan in meal_scans:
            scans.setdefault(f"day_{scan.day_number}", {})[scan.meal] = {
                "scanned": True,
                "scanned_at": scan.scanned_at,
            }
        data["mess"]["scans"] = scans

    accommodation = session.execute(
        select(
            AccommodationAllocation,
            Hostel.hostel_id,
            HostelRoom.room_number,
        )
        .join(HostelRoom, AccommodationAllocation.room_id == HostelRoom.id)
        .join(Hostel, HostelRoom.hostel_id == Hostel.id)
        .where(AccommodationAllocation.participant_id == participant.id)
    ).first()
    if accommodation:
        allocation, hostel_id, room_number = accommodation
        data["accommodation"].update({
            "hostel_id": hostel_id,
            "room": room_number,
            "inside": allocation.is_inside,
            "arrival": allocation.arrival_at.isoformat() if allocation.arrival_at else None,
            "departure": allocation.departure_at.isoformat() if allocation.departure_at else None,
        })
        data["accommodation"].setdefault("registered", True)
    payments = session.scalars(
        select(Payment)
        .where(Payment.participant_id == participant.id)
        .order_by(Payment.created_at.desc())
    ).all()
    for payment in payments:
        payment_view = {
            "paid": payment.status == "paid",
            "transaction_id": payment.transaction_id,
            "amount": payment.amount,
            "method": payment.method,
            "paid_at": payment.settled_at,
        }
        if payment.facility_type == "hostel" and "payment" not in data["accommodation"]:
            data["accommodation"]["payment"] = payment_view
        elif payment.facility_type == "mess" and "payment" not in data["mess"]:
            data["mess"]["payment"] = payment_view
    event_rows = session.scalars(
        select(EventRegistration)
        .where(EventRegistration.participant_id == participant.id,
               EventRegistration.status == "registered")
        .order_by(EventRegistration.registered_at)
    ).all()
    data["events"] = [
        {
            "event_id": str(registration.event_id) if registration.event_id else registration.event_identifier,
            "team_id": registration.team_id or registration.team_identifier,
            "team_role": (registration.attributes or {}).get("team_role", "member"),
            "registration_data": registration.answers or {},
        }
        for registration in event_rows
    ]
    workshop_rows = session.execute(
        select(WorkshopRegistration, Workshop.workshop_id)
        .join(Workshop, WorkshopRegistration.workshop_id == Workshop.id)
        .where(WorkshopRegistration.participant_id == participant.id,
               WorkshopRegistration.status == "registered")
    ).all()
    data["workshops"] = [
        {
            "workshop_id": workshop_key,
            "booking_type": registration.attributes.get("booking_type", "pre-registered"),
            "attended": session.scalar(select(WorkshopAttendance.id).where(
                WorkshopAttendance.workshop_id == registration.workshop_id,
                WorkshopAttendance.participant_id == participant.id).limit(1)) is not None,
            **(registration.attributes.get("api_fields") or {}),
        }
        for registration, workshop_key in workshop_rows
    ]
    return data


def staff_record(staff: StaffUser) -> dict[str, Any]:
    return {
        "_id": str(staff.id),
        "paradox_id": staff.paradox_id,
        "email": staff.email,
        "password_hash": staff.password_hash,
        "role": staff.role,
        "department": staff.department,
        "admin_id": str(staff.participant_id) if staff.participant_id else None,
        "created_at": staff.created_at,
        **(staff.attributes or {}),
    }


def get_participant_by_id(participant_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        row = session.scalar(select(Participant).where(Participant.participant_id == participant_id))
        return participant_record(session, row) if row else None


def get_staff_by_id(paradox_id: str) -> dict[str, Any] | None:
    with SessionLocal() as session:
        row = session.scalar(select(StaffUser).where(StaffUser.paradox_id == paradox_id))
        return staff_record(row) if row else None


def create_staff(data: dict[str, Any]) -> dict[str, Any]:
    linked_participant = data.get("admin_id")
    row = StaffUser(
        paradox_id=data["paradox_id"],
        email=data["email"],
        email_normalized=data["email"].strip().lower(),
        password_hash=data["password_hash"],
        role=data["role"],
        department=data.get("department"),
        participant_id=uuid.UUID(str(linked_participant)) if linked_participant else None,
        attributes={
            "name": data.get("name"),
            "designation": data.get("designation"),
        },
    )
    with SessionLocal.begin() as session:
        session.add(row)
        session.flush()
        return staff_record(row)


def list_staff() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        return [staff_record(row) for row in session.scalars(select(StaffUser).order_by(StaffUser.created_at)).all()]


def update_staff(paradox_id: str, values: dict[str, Any]) -> None:
    with SessionLocal.begin() as session:
        row = session.scalar(select(StaffUser).where(StaffUser.paradox_id == paradox_id))
        if row is None:
            return
        for key in ("name", "designation"):
            if key in values:
                row.attributes = {**(row.attributes or {}), key: values[key]}
        row.updated_at = datetime.now(timezone.utc)


def delete_staff(paradox_id: str) -> dict[str, Any] | None:
    with SessionLocal.begin() as session:
        row = session.scalar(select(StaffUser).where(StaffUser.paradox_id == paradox_id))
        if row is None:
            return None
        previous = staff_record(row)
        session.delete(row)
        return previous


def get_participant_by_email(email: str) -> dict[str, Any] | None:
    canonical = email.strip().lower()
    with SessionLocal() as session:
        row = session.scalar(select(Participant).where(Participant.email_normalized == canonical))
        return participant_record(session, row) if row else None


def get_staff_by_email(email: str) -> dict[str, Any] | None:
    canonical = email.strip().lower()
    with SessionLocal() as session:
        row = session.scalar(select(StaffUser).where(StaffUser.email_normalized == canonical))
        return staff_record(row) if row else None


def get_staff_linked_to_participant(participant_id: str) -> dict[str, Any] | None:
    try:
        internal_id = uuid.UUID(str(participant_id))
    except (ValueError, TypeError):
        return None
    with SessionLocal() as session:
        row = session.scalar(select(StaffUser).where(StaffUser.participant_id == internal_id))
        return staff_record(row) if row else None


def create_participant(data: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    row = Participant(
        participant_id=data["participant_id"],
        email=data["email"],
        email_normalized=data["email"].strip().lower(),
        password_hash=data["password_hash"],
        profile=data.get("profile") or {},
        photo=data.get("photo"),
        qr_secrets=data.get("qr_secrets") or {},
        embedding=data.get("embedding") or {},
        mess_state=data.get("mess") or {},
        accommodation_state=data.get("accommodation") or {},
        attributes={key: data[key] for key in ("events", "workshops") if key in data},
        created_at=data.get("created_at", now),
        updated_at=data.get("updated_at", now),
    )
    with SessionLocal.begin() as session:
        session.add(row)
        session.flush()
        return participant_record(session, row)


def update_last_login(record_id: str, *, staff: bool = False) -> None:
    with SessionLocal.begin() as session:
        try:
            internal_id = uuid.UUID(str(record_id))
        except (ValueError, TypeError):
            return
        row = session.get(StaffUser if staff else Participant, internal_id)
        if row:
            row.updated_at = datetime.now(timezone.utc)


def change_password(record_id: str, password_hash: str, *, staff: bool = False) -> None:
    internal_id = uuid.UUID(str(record_id))
    with SessionLocal.begin() as session:
        row = session.get(StaffUser if staff else Participant, internal_id)
        if row is None:
            return
        row.password_hash = password_hash
        row.updated_at = datetime.now(timezone.utc)
