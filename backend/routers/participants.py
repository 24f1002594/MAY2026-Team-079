from fastapi import APIRouter, HTTPException, Depends, Query
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select

from db import SessionLocal
from schema import (
    AccommodationAllocation, EventRegistration, Hostel, HostelRoom, MessAllocation,
    Participant, StaffUser, WorkshopRegistration,
)
from dependencies import get_current_staff
from logger import log_audit, log_denied
from models import PAGE_LIMIT_MAX, ParticipantAdminUpdateRequest

router = APIRouter(prefix="/participants", tags=["Participants"])


def _require_super_admin(current_user: dict, operation: str) -> str:
    """The shared Super Admin gate for this router, with the refusal recorded."""
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        is_admin = session.scalar(select(StaffUser.id).where(
            StaffUser.paradox_id == user_id, StaffUser.role == "super_admin"
        )) is not None
    if not is_admin:
        log_denied(
            current_user,
            "AUTHZ_DENIED",
            None,
            reason="not_super_admin",
            details={"operation": operation, "resource": "participants", "status": 403},
        )
        raise HTTPException(status_code=403, detail="Not authorized")
    return user_id


@router.get("/statistics")
def participant_statistics(current_user: dict = Depends(get_current_staff)):
    """
    Fest-wide participant counts, for the admin overview board.

    Counts only. No name, email, phone, address, photo, or participant id leaves
    this endpoint, so the dashboard can read fest-wide totals without exposing a
    roster — the per-entity ``/statistics`` endpoints remain the only way to see
    who is actually allotted where.

    Super Admins only, matching ``/mess/{id}/statistics`` and
    ``/hostels/{id}/statistics``.

    ``total_registered`` is every account in the ``participants`` collection,
    which is what ``POST /auth/register`` creates — a real registration total
    rather than a count of people who happen to have turned up somewhere.
    """
    user_id = _require_super_admin(current_user, "statistics")

    # Only the fields the counts below need. Password hashes, QR keypairs, and
    # every profile field that identifies somebody stay out of memory entirely.
    with SessionLocal() as session:
        participants = session.execute(select(
            Participant.id,
            Participant.full_name,
            Participant.house,
            Participant.gender,
            Participant.program,
            Participant.course_stage,
            Participant.mess_state,
            Participant.accommodation_state,
            Participant.created_at,
        )).all()
        event_counts = dict(session.execute(
            select(EventRegistration.participant_id, func.count())
            .group_by(EventRegistration.participant_id)
        ).all())
        workshop_counts = dict(session.execute(
            select(WorkshopRegistration.participant_id, func.count())
            .group_by(WorkshopRegistration.participant_id)
        ).all())
        mess_allocated = set(session.scalars(select(MessAllocation.participant_id)).all())
        accommodation_rows = session.execute(
            select(AccommodationAllocation.participant_id, AccommodationAllocation.is_inside)
        ).all()
        accommodation_by_participant = {participant_id: is_inside for participant_id, is_inside in accommodation_rows}

    total = len(participants)
    profile_complete = 0
    mess_registered = mess_allotted = 0
    hostel_registered = hostel_allotted = on_campus = 0
    with_events = with_workshops = 0

    houses: Counter = Counter()
    programs: Counter = Counter()
    stages: Counter = Counter()
    genders: Counter = Counter()
    signups_by_day: Counter = Counter()

    for participant in participants:
        # A profile exists as `{}` from registration until `PATCH /profile/complete`
        # fills it, so `full_name` is what separates "signed up" from "ready".
        if participant.full_name:
            profile_complete += 1
        if participant.house:
            houses[participant.house] += 1
        if participant.program:
            programs[participant.program] += 1
        if participant.course_stage:
            stages[participant.course_stage] += 1
        if participant.gender:
            genders[participant.gender] += 1

        mess = participant.mess_state or {}
        if mess.get("registered"):
            mess_registered += 1
        if participant.id in mess_allocated:
            mess_allotted += 1

        accommodation = participant.accommodation_state or {}
        if accommodation.get("registered"):
            hostel_registered += 1
        if participant.id in accommodation_by_participant:
            hostel_allotted += 1
        if accommodation_by_participant.get(participant.id, False):
            on_campus += 1

        if event_counts.get(participant.id, 0):
            with_events += 1
        if workshop_counts.get(participant.id, 0):
            with_workshops += 1

        created = participant.created_at
        if created is not None and hasattr(created, "strftime"):
            signups_by_day[created.strftime("%Y-%m-%d")] += 1

    return {
        "total_registered": total,
        "profile_complete": profile_complete,
        "profile_incomplete": total - profile_complete,
        "mess_registered": mess_registered,
        "mess_allotted": mess_allotted,
        "hostel_registered": hostel_registered,
        "hostel_allotted": hostel_allotted,
        # Never negative: allocation can only ever catch up with the queue.
        "hostel_pending": max(0, hostel_registered - hostel_allotted),
        "currently_on_campus": on_campus,
        "with_event_registrations": with_events,
        "with_workshop_registrations": with_workshops,
        "by_house": dict(houses),
        "by_program": dict(programs),
        "by_course_stage": dict(stages),
        "by_gender": dict(genders),
        # Chronological, so the client can render it as a trend without sorting
        # a dict whose key order it should not have to trust.
        "signups_by_day": dict(sorted(signups_by_day.items())),
    }


@router.get("")
def list_participants(
    q: Optional[str] = None,
    house: Optional[str] = None,
    limit: int = Query(200, ge=1, le=PAGE_LIMIT_MAX),
    current_user: dict = Depends(get_current_staff),
):
    """
    The fest-wide participant roster — Story 7.3's missing read half.

    Super Admins only, matching every other roster in the API. ``/statistics``
    above stays deliberately roster-free: a dashboard that shows totals to
    whoever can see the dashboard must not be the thing that leaks a list of
    names, so the two are separate endpoints rather than one endpoint with a
    flag.

    Projection is an allow-list. ``password_hash`` and ``qr_secrets`` are the two
    fields that must never leave this collection, and an inclusion projection
    means a field added to the schema later stays private until it is named here.
    ``photo`` and ``embedding`` are excluded for size, not secrecy — a roster of
    200 base64 photographs is a response nobody wants.

    ``q`` matches a name, email, or participant id, case-insensitively, so one
    search box finds a person however the admin knows them.
    """
    user_id = _require_super_admin(current_user, "list")

    with SessionLocal() as session:
        query = select(Participant)
        if house:
            query = query.where(Participant.house == house)
        if q and q.strip():
            term = f"%{q.strip()}%"
            query = query.where(
                Participant.participant_id.ilike(term)
                | Participant.email.ilike(term)
                | Participant.full_name.ilike(term)
            )
        rows = session.scalars(query.order_by(Participant.created_at.desc()).limit(limit)).all()
        participant_ids = [row.id for row in rows]
        event_counts = dict(session.execute(
            select(EventRegistration.participant_id, func.count())
            .where(EventRegistration.participant_id.in_(participant_ids))
            .group_by(EventRegistration.participant_id)
        ).all()) if participant_ids else {}
        workshop_counts = dict(session.execute(
            select(WorkshopRegistration.participant_id, func.count())
            .where(WorkshopRegistration.participant_id.in_(participant_ids))
            .group_by(WorkshopRegistration.participant_id)
        ).all()) if participant_ids else {}
        mess_rows = dict(session.execute(
            select(MessAllocation.participant_id, MessAllocation.mess_id)
            .where(MessAllocation.participant_id.in_(participant_ids))
        ).all()) if participant_ids else {}
        hostel_rows = session.execute(
            select(AccommodationAllocation.participant_id, AccommodationAllocation.is_inside, Hostel.hostel_id, HostelRoom.room_number)
            .join(HostelRoom, AccommodationAllocation.room_id == HostelRoom.id)
            .join(Hostel, HostelRoom.hostel_id == Hostel.id)
            .where(AccommodationAllocation.participant_id.in_(participant_ids))
        ).all() if participant_ids else []
        hostel_by_participant = {
            participant_id: {"inside": inside, "hostel_id": hostel_id, "room": room_number}
            for participant_id, inside, hostel_id, room_number in hostel_rows
        }
        participants = []
        for participant in rows:
            mess = dict(participant.mess_state or {})
            allocation_id = mess_rows.get(participant.id)
            if allocation_id:
                mess["mess_id"] = str(allocation_id)
            accommodation = dict(participant.accommodation_state or {})
            accommodation.update(hostel_by_participant.get(participant.id, {}))
            participants.append({
                "participant_id": participant.participant_id,
                "email": participant.email,
                "profile": participant.profile or {},
                "mess": mess,
                "accommodation": accommodation,
                "created_at": participant.created_at,
                "updated_at": participant.updated_at,
                "event_count": event_counts.get(participant.id, 0),
                "workshop_count": workshop_counts.get(participant.id, 0),
            })

    # Reading a roster is itself an auditable act. This response carries names,
    # email addresses, phone numbers, and addresses for up to `limit` people, and
    # the trail recorded who *changed* a participant while saying nothing about who
    # *read* every participant. The search term is recorded because "who looked up
    # this particular student, and when" is the question that follows a complaint
    # about staff misusing the roster; the returned rows themselves are not.
    log_audit(
        current_user,
        "READ_PARTICIPANT_ROSTER",
        None,
        {
            "search": q,
            "house": house,
            "limit": limit,
            "returned": len(participants),
        },
    )

    return {"count": len(participants), "participants": participants}


@router.patch("/{participant_id}")
def update_participant(
    participant_id: str,
    request: ParticipantAdminUpdateRequest,
    current_user: dict = Depends(get_current_staff),
):
    """
    Edit another person's record — Story 7.3's missing write half.

    Super Admins only. Until this existed there was no endpoint anywhere that
    wrote to a participant document other than the participant's own
    ``PATCH /profile/complete``, so an admin who spotted a misspelled name on a
    hostel roster could do nothing about it.

    Deliberately narrow. Only ``profile`` fields are writable:

    * ``email`` and ``participant_id`` are identity — ``participant_id`` is
      derived from the email by ``generate_participant_id`` and is the key every
      roster, log row, and QR payload joins on.
    * ``password_hash`` and ``qr_secrets`` are credentials.
    * ``mess`` / ``accommodation`` / ``events`` / ``workshops`` are owned by the
      allocation and registration routes, which enforce capacity and state. An
      admin writing them directly would put a participant in a hall with no seat
      left, or mark them inside a block the scanner thinks they left.

    Every field is optional and only the ones present are written, so a form that
    fixes a phone number cannot blank an address.
    """
    _require_super_admin(current_user, "update")

    with SessionLocal() as session:
        participant = session.scalar(select(Participant).where(Participant.participant_id == participant_id))
    if not participant:
        log_denied(
            current_user,
            "UPDATE_PARTICIPANT_DENIED",
            participant_id,
            reason="participant_not_found",
            details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Participant not found")

    supplied = request.model_dump(exclude_unset=True, exclude_none=True)
    if not supplied:
        log_denied(
            current_user,
            "UPDATE_PARTICIPANT_DENIED",
            participant_id,
            reason="nothing_to_update",
            details={"status": 400},
            audit=False,
        )
        raise HTTPException(status_code=400, detail="Nothing to update")

    profile = dict(participant.profile or {})
    profile.update({
        key: value.model_dump() if hasattr(value, "model_dump") else value
        for key, value in supplied.items()
    })
    with SessionLocal.begin() as session:
        row = session.scalar(select(Participant).where(Participant.participant_id == participant_id).with_for_update())
        if row is None:
            raise HTTPException(status_code=404, detail="Participant not found")
        row.profile = profile
        for field in ("full_name", "house", "gender", "phone", "country", "state", "city", "address", "program", "course_stage", "mess_preference"):
            if field in supplied:
                setattr(row, field, supplied[field])
        row.updated_at = datetime.now(timezone.utc)
    log_audit(current_user, "UPDATE_PARTICIPANT", participant_id, {"fields_updated": sorted(supplied.keys())})

    return {"message": "Participant updated", "profile": profile}
