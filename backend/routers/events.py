"""
Events — creation, registration, team management, attendance scanning, and
announcements.

Schema (restructured, no backward compatibility with the previous shape — see
docs/events_detailed_documentation.md for the full document layout):

    {
      event_id, event_type, name, description, embedding, poster,
      team: {min, max, house_vs_house_event, allow_single_registration},
      prize_money: [{position, amount}],
      registration: {start_time, end_time, allowed},
      schedule: [{round_id, name, description, start_time, end_time, venue}],
      registration_fields: [{field_id, label, type, required}],
      event_team: [{user_id, role}],       # role: event_head | member | volunteer
      announcements: [{announcement_id, message, priority, created_by, created_at}],
      created_by, created_at, updated_at
    }

Two things that used to live on the event document do not any more:

  * The registration roster mirror (the old `logs` array). Registration state
    was already the source of truth on `participants.events[]`; the mirror
    only ever grew, and every reader that needed a count already queried
    `participants_collection` directly (`event_capacity`, `view_participation`).
    Removing it does not remove any capability — it removes a second, easily
    drifting copy of the same fact.
  * Attendance scans and audit actions were already logged elsewhere
    (`event_logs_collection`, `system_logs_collection` via `log_audit`) and are
    unaffected by this file.

Team membership constraint: a `backend_teams` user may sit on at most one
event's `event_team`, enforced across the whole collection, not just within
one event. A team member also cannot register as a participant for the event
they are on the team of (unchanged from before, still enforced below).

Registration open/closed is never physically flipped by a background job:
`_registration_open` computes it on every read, from `registration.allowed`
(a Super Admin kill-switch) AND the current time being inside
`[start_time, end_time]`. Both conditions must hold.

A participant registers exactly one of two ways — never both at once
(`EventRegistrationInput._create_xor_join` rejects a request naming both a
`team_name` and a `team_id`):

  * Solo: neither `team_name` nor `team_id` set. Refused with 400 if the
    event's `team.allow_single_registration` is false and `team.max` > 1.
  * As a team: `team_name` creates a new team and becomes its leader — its
    `team_id` is backend-assigned via `EventIDGenerator.next_team_id`, the
    same generator that assigns `event_id` and `round_id`, never chosen by
    the client. `team_id` joins an existing team as a member, refused with
    404/400 if that team does not exist for this event or is already at
    `team.max`. See `_resolve_registration_team`.
"""
import asyncio
import json
import uuid
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from logger import (
    OUTCOME_ALLOWED, OUTCOME_DENIED, OUTCOME_DUPLICATE,
    log_audit, log_batch, log_denied, log_integrity, log_scan,
)
from typing import Literal, Optional, List
from datetime import datetime, timezone
import random
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import log_config

from models import (
    EventCreateRequest,
    EventUpdateRequest,
    EventRegistrationInput,
    EventTeamAssignRequest,
    EventTeamRoleUpdateRequest,
    AnnouncementCreateRequest,
    ScanQRRequest,
    parse_instant_utc,
)
from db import SessionLocal
from schema import Event, EventAttendance, EventLog, EventRegistration, EventStaff, Participant, ParticipantTeam, ParticipantTeamMember, StaffUser
from dependencies import get_current_user, get_current_staff, get_current_participant, verify_qr
from embedding_service import generate_embedding
from id_generator import EVENT_TYPE_CODES, EventIDGenerator

generator = EventIDGenerator()

router = APIRouter(prefix="/events", tags=["Events"])

_log = log_config.get_logger("paradox.events")


# ── shared helpers ───────────────────────────────────────────────────────────

def _is_super_admin(current_user: dict) -> bool:
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        is_admin = session.scalar(select(StaffUser.id).where(
            StaffUser.paradox_id == user_id, StaffUser.role == "super_admin"
        )) is not None
    return bool(is_admin or current_user.get("role") == "super_admin")


def _event_record(session, row: Event, *, include_private: bool = True) -> dict:
    attrs = row.attributes or {}
    members = session.execute(
        select(EventStaff, StaffUser)
        .join(StaffUser, StaffUser.id == EventStaff.staff_id)
        .where(EventStaff.event_id == row.id)
        .order_by(StaffUser.paradox_id)
    ).all()
    data = {
        "_id": str(row.id),
        "event_id": row.event_id,
        "event_type": row.category,
        "name": row.name,
        "description": attrs.get("description", ""),
        "embedding": attrs.get("embedding", {}),
        "poster": attrs.get("poster"),
        "team": attrs.get("team") or {},
        "prize_money": attrs.get("prize_money") or [],
        "registration": row.registration_config or {},
        "schedule": row.schedule or [],
        "registration_fields": attrs.get("registration_fields") or [],
        "event_team": [
            {"user_id": staff.paradox_id, "role": membership.role}
            for membership, staff in members
        ],
        "announcements": attrs.get("announcements") or [],
        "created_by": attrs.get("created_by"),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if not include_private:
        data.pop("_id", None)
        data.pop("created_by", None)
        data.pop("event_team", None)
        data.pop("registration_fields", None)
        data.pop("announcements", None)
    return data


def _require_super_admin(current_user: dict) -> None:
    if not _is_super_admin(current_user):
        log_denied(
            current_user,
            "AUTHZ_DENIED",
            None,
            reason="not_super_admin",
            details={"resource": "events", "status": 403},
        )
        raise HTTPException(status_code=403, detail="Only Super Admins can perform this action")


def _registration_open(event: dict) -> bool:
    """
    Effective open/closed state: the manual override AND the time window,
    both required. Neither alone is authoritative — an admin can force-close
    early with `allowed=False`, but cannot force registration open outside the
    published window.
    """
    registration = event.get("registration") or {}
    if not registration.get("allowed", True):
        return False
    start = registration.get("start_time")
    end = registration.get("end_time")
    if not start or not end:
        return False
    try:
        start_dt = parse_instant_utc(start, "start_time")
        end_dt = parse_instant_utc(end, "end_time")
    except ValueError:
        # Registration reads as closed forever, for every participant, with no
        # symptom other than an event nobody can sign up for. An admin looking at
        # the event sees `allowed: true` and a window that has not passed, and no
        # explanation anywhere. This is the explanation.
        log_integrity(
            "event registration treated as closed: its window will not parse",
            reason="registration_window_unparseable",
            details={
                "event_id": event.get("event_id"),
                "start_type": type(start).__name__,
                "end_type": type(end).__name__,
            },
        )
        return False
    now = datetime.utcnow()
    return start_dt <= now <= end_dt


def _with_computed_registration(event: dict) -> dict:
    """Attaches `registration.is_open` without persisting it — it is derived,
    never stored, so it can never drift from the fields it is derived from."""
    registration = dict(event.get("registration") or {})
    registration["is_open"] = _registration_open(event)
    event = dict(event)
    event["registration"] = registration
    return event


def _stored_event_type(event: dict) -> str:
    """
    This event's `event_type`, checked before it reaches an id generator.

    Every id this module mints — round ids, team ids — derives its prefix from the
    event type, and for a *stored* event that value has never been validated by any
    request model: `EventCreateRequest.event_type` is a `Literal`, but a document
    written by a migration or by hand is not bound by it.

    Reading it back and handing it straight to the generator therefore turned bad
    stored data into an opaque 500 (an `UnboundLocalError` on an unassigned prefix
    variable). Resolved once, here, so the four call sites read the same and a corrupt
    document reports which field is wrong instead of crashing.
    """
    event_type = event.get("event_type", "others")
    if event_type not in EVENT_TYPE_CODES:
        log_integrity(
            "event has an event_type no id generator recognises",
            reason="stored_event_type_unknown",
            details={
                "event_id": event.get("event_id"),
                "event_type": str(event_type),
                "known_types": sorted(EVENT_TYPE_CODES),
            },
        )
        raise HTTPException(
            status_code=422,
            detail=f"This event's event_type {event_type!r} is not one of "
                   f"{sorted(EVENT_TYPE_CODES)}; it cannot be used to generate ids",
        )
    return event_type


def _event_team_role(event: dict, user_id: str) -> Optional[str]:
    for member in event.get("event_team", []):
        if str(member.get("user_id")) == str(user_id):
            return member.get("role")
    return None


def _is_event_team_member(event: dict, user_id: str) -> bool:
    return _event_team_role(event, user_id) is not None


def _is_event_head(event: dict, user_id: str) -> bool:
    return _event_team_role(event, user_id) == "event_head"


# ── create / read ────────────────────────────────────────────────────────────

@router.post("")
def create_event(request: EventCreateRequest, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user)

    schedule_data = []
    for rnd in request.schedule:
        schedule_data.append({
            "round_id": generator.next_round_id(request.event_type),
            "name": rnd.name,
            "description": rnd.description,
            "start_time": rnd.start_time,
            "end_time": rnd.end_time,
            "venue": rnd.venue
        })

    event_id = generator.next_event_id(request.event_type)
    now = datetime.now(timezone.utc)
    try:
        with SessionLocal.begin() as session:
            session.add(Event(
                event_id=event_id,
                category=request.event_type,
                name=request.name,
                capacity=None,
                registration_config=request.registration.model_dump(mode="json"),
                schedule=schedule_data,
                attributes={
                    "description": request.description,
                    "embedding": generate_embedding(request.description),
                    "poster": request.poster,
                    "team": request.team.model_dump(mode="json"),
                    "prize_money": [pm.model_dump(mode="json") for pm in request.prize_money],
                    "registration_fields": [rf.model_dump(mode="json") for rf in request.registration_fields],
                    "announcements": [],
                    "created_by": current_user["_id"],
                    "updated_at": now.isoformat(),
                },
                created_at=now,
            ))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Event id already exists") from exc
    log_audit(current_user, "CREATE_EVENT", event_id, {"event_name": request.name})
    return {"message": "Event created", "event_id": event_id}


@router.get("")
def list_events(current_user: dict = Depends(get_current_user)):
    """
    Internal creator UUIDs are not part of the public response. Registration
    state lives in the relational `event_registrations` table.

    `registration.is_open` is attached per event without being stored, so it
    reflects `registration.allowed` and the time window at read time.
    """
    with SessionLocal() as session:
        rows = session.scalars(select(Event).order_by(Event.created_at, Event.event_id)).all()
        events = [_event_record(session, row, include_private=False) for row in rows]
    return [_with_computed_registration(e) for e in events]


# Allow-list of the fields that make up the published festival brochure.
# Written as an inclusion projection on purpose: any field added to the events
# collection later stays private until it is named here explicitly.
PUBLIC_EVENT_FIELDS = {
    "_id": 0,
    "event_id": 1,
    "event_type": 1,
    "name": 1,
    "description": 1,
    "embedding": 1,
    "poster": 1,
    "team": 1,
    "prize_money": 1,
    "registration": 1,
    "schedule": 1,
}


@router.get("/public")
def list_public_events():
    """
    The festival brochure — every event, readable without signing in.

    Deliberately unauthenticated: this is the pre-login events catalogue the
    landing page renders, and it must work for a visitor with no account. Only
    the published fields above are returned — never `event_team` (staff
    identities), `registration_fields`, `announcements`, or internal
    bookkeeping.

    Declared before any `/{event_id}` route so the literal path is not
    captured as an event id.
    """
    with SessionLocal() as session:
        rows = session.scalars(select(Event).order_by(Event.created_at, Event.event_id)).all()
        events = [_event_record(session, row, include_private=False) for row in rows]
    return [_with_computed_registration(e) for e in events]


# ── update / delete ──────────────────────────────────────────────────────────

@router.put("/{event_id}")
def update_event(event_id: str, request: EventUpdateRequest, current_user: dict = Depends(get_current_staff)):
    # Authorise first, resolve second. The other order let any valid staff token
    # tell a real event id from an invented one — 404 versus 403 — before being
    # refused, which every sibling route in this module avoids by gating first.
    # This gate does not depend on the event, so there is nothing to look up yet.
    _require_super_admin(current_user)

    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    update_data = {k: v for k, v in request.model_dump(exclude_unset=True).items() if v is not None}

    if "team" in update_data:
        update_data["team"] = request.team.model_dump()
    if "prize_money" in update_data:
        update_data["prize_money"] = [pm.model_dump() for pm in request.prize_money]
    if "registration" in update_data:
        # `RegistrationWindowUpdate` is partial — merge onto what's stored so a
        # request that only flips `allowed` cannot blank `start_time`/`end_time`.
        merged = dict(event.get("registration") or {})
        merged.update({k: v for k, v in request.registration.model_dump().items() if v is not None})
        if not merged.get("start_time") or not merged.get("end_time"):
            raise HTTPException(status_code=422, detail="registration.start_time and end_time are required")
        if parse_instant_utc(merged["end_time"], "end_time") <= parse_instant_utc(merged["start_time"], "start_time"):
            raise HTTPException(status_code=422, detail="registration.end_time must be after start_time")
        update_data["registration"] = merged
    if "schedule" in update_data:
        update_data["schedule"] = [
            {
                "round_id": rnd.round_id or generator.next_round_id(_stored_event_type(event)),
                "name": rnd.name,
                "description": rnd.description,
                "start_time": rnd.start_time,
                "end_time": rnd.end_time,
                "venue": rnd.venue,
            }
            for rnd in request.schedule
        ]
    if "registration_fields" in update_data:
        update_data["registration_fields"] = [rf.model_dump() for rf in request.registration_fields]

    if "description" in update_data and event.get("description") != update_data["description"]:
        update_data["embedding"] = generate_embedding(update_data["description"])

    if update_data:
        update_data["updated_at"] = datetime.now(timezone.utc)
        with SessionLocal.begin() as session:
            event_row = session.scalar(select(Event).where(Event.event_id == event_id).with_for_update())
            if event_row:
                attrs = dict(event_row.attributes or {})
                for key in ("description", "embedding", "poster", "team", "prize_money", "registration_fields"):
                    if key in update_data:
                        value = update_data[key]
                        if isinstance(value, datetime):
                            value = value.isoformat()
                        attrs[key] = value
                if "name" in update_data:
                    event_row.name = update_data["name"]
                if "registration" in update_data:
                    event_row.registration_config = update_data["registration"]
                if "schedule" in update_data:
                    event_row.schedule = update_data["schedule"]
                event_row.attributes = attrs
                event_row.updated_at = update_data["updated_at"]
    log_audit(current_user, "UPDATE_EVENT", event_id, {"fields_updated": list(update_data.keys())})
    return {"message": "Event updated successfully"}


@router.delete("/{event_id}")
def delete_event(event_id: str, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user)

    # A mistyped id used to answer 200 "Event deleted" and write a DELETE_EVENT
    # row all the same, so the trail recorded a deletion that never happened and
    # the client could not tell it from a real one. Every other write in this
    # module 404s on an unknown event; the destructive one now does too.
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if not event:
        log_denied(
            current_user, "DELETE_EVENT_DENIED", event_id,
            reason="event_not_found",
            details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Event not found")

    with SessionLocal.begin() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id).with_for_update())
        registrations_removed = session.scalar(select(func.count()).select_from(EventRegistration)
                                               .where(EventRegistration.event_id == event_row.id)) or 0
        session.delete(event_row)
    log_audit(current_user, "DELETE_EVENT", event_id, {
        "name": event.get("name"),
        "event_type": event.get("event_type"),
        "registrations_removed": registrations_removed,
    })
    return {"message": "Event deleted"}


# ── team management ──────────────────────────────────────────────────────────

@router.post("/{event_id}/team")
def assign_event_team(event_id: str, request: EventTeamAssignRequest, current_user: dict = Depends(get_current_staff)):
    """
    Add a staff member to this event's team.

    A `user_id` must reference an existing `backend_teams` account — an event
    team names people who can scan, allocate teams, and post announcements,
    and an id nobody holds would grant those to nothing.

    One person, one event: enforced by refusing the assignment if `user_id` is
    already on *any* event's team, this one included (an existing member is
    changed via `PATCH .../team/{user_id}`, not re-added via `POST`).
    """
    _require_super_admin(current_user)

    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
        staff = session.scalar(select(StaffUser).where(StaffUser.paradox_id == request.user_id))
        existing_membership = session.scalar(select(EventStaff).where(EventStaff.staff_id == staff.id)) if staff else None
        existing_event = session.get(Event, existing_membership.event_id) if existing_membership else None
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if not staff:
        raise HTTPException(status_code=404, detail="user_id must reference an existing backend_teams member")

    if existing_event:
        if existing_event.event_id == event_id:
            raise HTTPException(
                status_code=409,
                detail="Already on this event's team; use PATCH to change their role",
            )
        raise HTTPException(
            status_code=409,
            detail=f"user_id is already on the team of event {existing_event.event_id}; "
                   "a person may be on only one event's team",
        )

    try:
        with SessionLocal.begin() as session:
            event_row = session.scalar(select(Event).where(Event.event_id == event_id).with_for_update())
            staff_row = session.scalar(select(StaffUser).where(StaffUser.paradox_id == request.user_id))
            session.add(EventStaff(event_id=event_row.id, staff_id=staff_row.id, role=request.role))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="user_id is already on the team of another event") from exc
    log_audit(current_user, "ASSIGN_EVENT_TEAM", event_id, {"assigned_user": request.user_id, "role": request.role})
    return {"message": "Team member assigned"}


@router.patch("/{event_id}/team/{team_user_id}")
def update_event_team_role(
    event_id: str,
    team_user_id: str,
    request: EventTeamRoleUpdateRequest,
    current_user: dict = Depends(get_current_staff),
):
    _require_super_admin(current_user)

    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if not _is_event_team_member(event, team_user_id):
        raise HTTPException(status_code=404, detail="user_id is not on this event's team")

    with SessionLocal.begin() as session:
        membership = session.scalar(
            select(EventStaff).join(Event, Event.id == EventStaff.event_id)
            .join(StaffUser, StaffUser.id == EventStaff.staff_id)
            .where(Event.event_id == event_id, StaffUser.paradox_id == team_user_id)
        )
        if membership:
            membership.role = request.role
    log_audit(current_user, "UPDATE_EVENT_TEAM_ROLE", event_id, {"team_user_id": team_user_id, "role": request.role})
    return {"message": "Team member role updated"}


@router.delete("/{event_id}/team/{team_user_id}")
def remove_event_team_member(event_id: str, team_user_id: str, current_user: dict = Depends(get_current_staff)):
    """Frees this person up to be assigned to a different event's team."""
    _require_super_admin(current_user)

    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    if not _is_event_team_member(event, team_user_id):
        raise HTTPException(status_code=404, detail="user_id is not on this event's team")

    with SessionLocal.begin() as session:
        membership = session.scalar(
            select(EventStaff).join(Event, Event.id == EventStaff.event_id)
            .join(StaffUser, StaffUser.id == EventStaff.staff_id)
            .where(Event.event_id == event_id, StaffUser.paradox_id == team_user_id)
        )
        if membership:
            session.delete(membership)
    log_audit(current_user, "REMOVE_EVENT_TEAM_MEMBER", event_id, {"team_user_id": team_user_id})
    return {"message": "Team member removed"}


# ── registration ─────────────────────────────────────────────────────────────

def _validate_registration_data(event: dict, registration_data: dict) -> None:
    """Every `registration_fields` entry marked `required` must be present and
    non-empty in what the participant submitted."""
    registration_data = registration_data or {}
    missing = []
    for field in event.get("registration_fields", []):
        if not field.get("required"):
            continue
        field_id = field.get("field_id")
        value = registration_data.get(field_id)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field.get("label") or field_id)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Missing required registration field(s): {', '.join(missing)}",
        )


def _team_size(event: dict, team_id: str) -> int:
    """How many participants are currently registered under this team_id for
    this event. Team membership lives only on `participants.events[]` — there
    is no roster mirror on the event document — so counting means querying
    the participants collection, the same source `event_capacity` and
    `view_participation` already read from."""
    with SessionLocal() as session:
        return session.scalar(
            select(func.count()).select_from(EventRegistration)
            .where(
                EventRegistration.event_id == uuid.UUID(str(event["_id"])),
                EventRegistration.team_id == team_id,
            )
        ) or 0


def _remove_participant_team_link(session, event_uuid: uuid.UUID, team_id: str, participant_uuid: uuid.UUID) -> None:
    team = session.scalar(select(ParticipantTeam).where(
        ParticipantTeam.event_id == event_uuid,
        ParticipantTeam.team_id == team_id,
    ).with_for_update())
    if team is None:
        return
    member = session.get(ParticipantTeamMember, (team.id, participant_uuid))
    if member:
        session.delete(member)
        session.flush()
    remaining = session.scalars(select(ParticipantTeamMember).where(
        ParticipantTeamMember.team_id == team.id
    ).order_by(ParticipantTeamMember.joined_at)).all()
    if not remaining:
        session.delete(team)
    elif team.leader_id == participant_uuid:
        team.leader_id = remaining[0].participant_id
        remaining[0].role = "leader"


def _resolve_registration_team(event: dict, reg_input: Optional[EventRegistrationInput]) -> tuple:
    """
    Solo vs. team, decided once, in one place, so `register_for_event` cannot
    drift between what it validates and what it writes.

    Returns ``(team_id, team_role)`` — both ``None``/``"solo"`` for a solo
    registration. Raises the same `HTTPException`s the route used to raise
    inline.

    A team_name and a team_id are mutually exclusive at the schema layer
    already (`EventRegistrationInput._create_xor_join`); everything here is
    validation that depends on *this event's* rules, which the model itself
    has no way to see.
    """
    team_rules = event.get("team", {})
    max_size = team_rules.get("max", 1)
    allow_solo = team_rules.get("allow_single_registration", True)

    team_name = reg_input.team_name if reg_input else None
    team_id = reg_input.team_id if reg_input else None

    if (team_name or team_id) and max_size <= 1:
        raise HTTPException(
            status_code=400,
            detail="This event does not support team registration",
        )

    if team_name:
        # Creating a team. The id is backend-assigned — the same way event_id
        # and round_id are — so `team_name` is stored purely as a display
        # label alongside it, never used as the id itself.
        new_team_id = generator.next_team_id(_stored_event_type(event))
        return new_team_id, "leader"

    if team_id:
        current_size = _team_size(event, team_id)
        if current_size == 0:
            raise HTTPException(status_code=404, detail="No team found with that team_id for this event")
        if current_size >= max_size:
            raise HTTPException(status_code=400, detail="This team is already full")
        return team_id, "member"

    # Solo — `team_role` stays "member" (no team_id) for this case, matching
    # the label already used for anyone on a team who isn't its leader.
    if not allow_solo and max_size > 1:
        raise HTTPException(
            status_code=400,
            detail="This event requires team registration; provide team_name to create a "
                   "team or team_id to join one",
        )
    return None, "member"


@router.post("/{event_id}/register")
def register_for_event(event_id: str, reg_input: Optional[EventRegistrationInput] = None, current_user: dict = Depends(get_current_participant)):
    if "participant_id" not in current_user:
        raise HTTPException(status_code=400, detail="Only participants can register for events")

    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    if not _registration_open(event):
        raise HTTPException(status_code=400, detail="Registration is closed for this event")

    if any(str(ev.get("event_id")) == str(event["_id"]) for ev in current_user.get("events", [])):
        raise HTTPException(status_code=409, detail="User is already registered for this event.")

    # Block event team members from registering as participants for their own event.
    # staff_users.participant_id is a foreign key to the participant row.
    participant_uuid = uuid.UUID(str(current_user["_id"]))
    with SessionLocal() as session:
        staff_row = session.scalar(select(StaffUser).where(StaffUser.participant_id == participant_uuid))
        on_event_team = bool(staff_row and session.scalar(select(EventStaff.event_id).where(
            EventStaff.event_id == event_row.id, EventStaff.staff_id == staff_row.id
        )))
    if on_event_team:
        raise HTTPException(
            status_code=403,
            detail="Event team members cannot register as participants for their own event."
        )

    registration_data = reg_input.registration_data if reg_input else {}
    _validate_registration_data(event, registration_data)

    team_id, team_role = _resolve_registration_team(event, reg_input)

    try:
        with SessionLocal.begin() as session:
            event_locked = session.scalar(select(Event).where(Event.id == event_row.id).with_for_update())
            participant = session.get(Participant, participant_uuid, with_for_update=True)
            if session.scalar(select(EventRegistration.id).where(
                EventRegistration.event_id == event_locked.id,
                EventRegistration.participant_id == participant.id,
            )):
                raise HTTPException(status_code=409, detail="User is already registered for this event.")
            if team_id:
                team = session.scalar(select(ParticipantTeam).where(
                    ParticipantTeam.event_id == event_locked.id,
                    ParticipantTeam.team_id == team_id,
                ).with_for_update())
                if team_role == "member" and team is None:
                    raise HTTPException(status_code=404, detail="No team found with that team_id for this event")
                current_size = session.scalar(select(func.count()).select_from(EventRegistration).where(
                    EventRegistration.event_id == event_locked.id,
                    EventRegistration.team_id == team_id,
                )) or 0
                if team_role == "member" and current_size >= (event.get("team") or {}).get("max", 1):
                    raise HTTPException(status_code=400, detail="This team is already full")
                if team is None:
                    team = ParticipantTeam(
                        event_id=event_locked.id,
                        team_id=team_id,
                        leader_id=participant.id,
                        attributes={"name": reg_input.team_name if reg_input else None},
                    )
                    session.add(team)
                    session.flush()
                session.add(ParticipantTeamMember(
                    team_id=team.id,
                    participant_id=participant.id,
                    role="leader" if team_role == "leader" else "member",
                ))
            session.add(EventRegistration(
                participant_id=participant.id,
                event_id=event_locked.id,
                event_identifier=event_locked.event_id,
                status="registered",
                team_id=team_id,
                team_identifier=team_id,
                answers=registration_data or {},
                attributes={"team_role": team_role},
            ))
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="User is already registered for this event.") from exc
    log_audit(current_user, "EVENT_REGISTER", event_id, {"team_id": team_id, "team_role": team_role})

    response = {"message": "Registered for event successfully.", "team_role": team_role}
    if team_id:
        response["team_id"] = team_id
    return response


@router.put("/{event_id}/register")
def edit_event_registration(event_id: str, reg_input: EventRegistrationInput, current_user: dict = Depends(get_current_participant)):
    if "participant_id" not in current_user:
        raise HTTPException(status_code=400, detail="Only participants can edit event registrations")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    if not _registration_open(event):
        raise HTTPException(status_code=400, detail="Registration is closed")

    _validate_registration_data(event, reg_input.registration_data)

    with SessionLocal.begin() as session:
        registration = session.scalar(select(EventRegistration).where(
            EventRegistration.participant_id == uuid.UUID(str(current_user["_id"])),
            EventRegistration.event_id == event_row.id,
        ).with_for_update())
        if registration:
            registration.answers = reg_input.registration_data or {}
    if registration is None:
        log_denied(
            current_user, "EVENT_REGISTRATION_EDIT_DENIED", event_id,
            reason="not_registered_for_event",
            details={"participant_id": current_user.get("participant_id")},
        )
        raise HTTPException(status_code=404, detail="Not registered for this event")

    # `EVENT_REGISTER` and `EVENT_DEREGISTER` were both audited; the edit in between
    # was not, so a registration's answers could be rewritten with no record. Field
    # *names* only — `registration_data` holds whatever the event asked for, which
    # for many events is personal information.
    log_audit(current_user, "EVENT_REGISTRATION_EDIT", event_id, {
        "participant_id": current_user.get("participant_id"),
        "fields_updated": sorted((reg_input.registration_data or {}).keys()),
    })
    return {"message": "Registration updated"}


@router.delete("/{event_id}/register")
def deregister_event(event_id: str, current_user: dict = Depends(get_current_participant)):
    if "participant_id" not in current_user:
        raise HTTPException(status_code=400, detail="Only participants can deregister")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    if not _registration_open(event):
        raise HTTPException(status_code=400, detail="Registration is closed")

    with SessionLocal.begin() as session:
        registration = session.scalar(select(EventRegistration).where(
            EventRegistration.participant_id == uuid.UUID(str(current_user["_id"])),
            EventRegistration.event_id == event_row.id,
        ).with_for_update())
        if registration:
            team_id = registration.team_id
            if team_id:
                team = session.scalar(select(ParticipantTeam).where(
                    ParticipantTeam.event_id == event_row.id,
                    ParticipantTeam.team_id == team_id,
                ).with_for_update())
                if team:
                    member = session.get(ParticipantTeamMember, (team.id, registration.participant_id))
                    if member:
                        session.delete(member)
            session.delete(registration)
            session.flush()
            if team_id and team:
                remaining = session.scalars(select(ParticipantTeamMember).where(
                    ParticipantTeamMember.team_id == team.id
                ).order_by(ParticipantTeamMember.joined_at)).all()
                if not remaining:
                    session.delete(team)
                elif team.leader_id == uuid.UUID(str(current_user["_id"])):
                    team.leader_id = remaining[0].participant_id
                    remaining[0].role = "leader"
    log_audit(current_user, "EVENT_DEREGISTER", event_id, {
        # The team they were in, captured before the pull removes it. Losing a member
        # can drop a team below `team.min`, and afterwards nothing links this person
        # to the team they left.
        "participant_id": current_user.get("participant_id"),
        "team_id": next(
            (
                ev.get("team_id")
                for ev in current_user.get("events") or []
                if str(ev.get("event_id")) == str(event["_id"])
            ),
            None,
        ),
    })
    return {"message": "Deregistered successfully"}


@router.get("/my_registrations")
def my_registrations(current_user: dict = Depends(get_current_participant)):
    """
    This participant's own event registrations.

    Registration rows reference the event by its internal UUID. This projection
    resolves each row to the public event identifier and name used by the UI.

    `event_id`/`name` are `None` when the event was deleted after registering
    (mirrors the workshop route's same behaviour for a deleted workshop) —
    `team_id`/`team_role`/`registration_data` survive either way.
    """
    if "participant_id" not in current_user:
        return []

    with SessionLocal() as session:
        rows = session.execute(
            select(EventRegistration, Event)
            .outerjoin(Event, Event.id == EventRegistration.event_id)
            .where(EventRegistration.participant_id == uuid.UUID(str(current_user["_id"])))
            .order_by(EventRegistration.registered_at.desc())
        ).all()
        return [{
            "event_id": event.event_id if event else None,
            "name": event.name if event else None,
            "team_id": registration.team_id or registration.team_identifier,
            "team_role": (registration.attributes or {}).get("team_role"),
            "registration_data": registration.answers,
        } for registration, event in rows]


def _unique_attendance_today(event: dict) -> int:
    """
    How many distinct participants have been scanned in today.

    ``POST /events/{event_id}/scan`` dedupes on
    ``(event, participant, scanner, day)`` — *including the scanner* — so that
    each volunteer keeps an accurate tally of their own gate in
    ``my_daily_scans``. The side effect is that one participant admitted by two
    volunteers writes two rows, and simply counting rows reported a half-empty
    venue as full.

    Counting distinct ``participant_id`` values instead keeps both readings
    correct: the per-scanner rows stay exactly as they were, so ``my_daily_scans``
    and the ``logs`` audit trail are untouched, while every *attendance* figure
    counts heads.
    """
    with SessionLocal() as session:
        return session.scalar(
            select(func.count(func.distinct(EventAttendance.participant_id)))
            .where(
                EventAttendance.event_id == uuid.UUID(str(event["_id"])),
                EventAttendance.service_date == datetime.now(timezone.utc).date(),
            )
        ) or 0


@router.get("/{event_id}/capacity")
def event_capacity(event_id: str, current_user: dict = Depends(get_current_user)):
    """
    How full this event is right now, as counts and nothing else — Story 3.3.

    This is deliberately the only fullness figure a *participant* can read. Every
    other one is staff-gated because every other one returns identities:
    ``participation`` needs ``get_current_staff`` and hands back the roster,
    ``logs`` needs ``super_admin`` and hands back the scan rows. A participant
    deciding whether to walk to a venue needs neither — they need two integers.

    So this returns two integers. No ``participant_id``, no name, no email, no
    registration data, for anybody. It is safe to expose precisely because there
    is nothing in it to leak.

    ``registered`` is counted from the participants collection, which
    ``deregister_event`` pulls from, so it falls when somebody cancels rather
    than only ever rising.

    ``attended_today`` counts distinct participants, not scan rows — see
    ``_unique_attendance_today``.

    The published capacity is **not** returned. It already rides in the event's
    ``registration`` map, which both ``GET /events`` and ``GET /events/public``
    already return in full, so the client that asks this question is holding it
    already. Parsing it in a second place is how two places come to disagree.
    """
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
        registrations = session.scalar(select(func.count()).select_from(EventRegistration)
                                       .where(EventRegistration.event_id == event_row.id)) if event_row else 0
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    return {
        "event_id": event_id,
        "registered": registrations,
        "attended_today": _unique_attendance_today(event)
    }


@router.get("/{event_id}/participation")
def view_participation(event_id: str, current_user: dict = Depends(get_current_staff)):
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
        admin_doc = session.scalar(select(StaffUser).where(StaffUser.paradox_id == user_id))
        registration_rows = session.execute(
            select(EventRegistration, Participant)
            .join(Participant, Participant.id == EventRegistration.participant_id)
            .where(EventRegistration.event_id == event_row.id)
            .order_by(Participant.participant_id)
        ).all() if event_row else []
        team_rows = session.execute(
            select(EventStaff, StaffUser, Participant)
            .join(StaffUser, StaffUser.id == EventStaff.staff_id)
            .outerjoin(Participant, Participant.id == StaffUser.participant_id)
            .where(EventStaff.event_id == event_row.id)
        ).all() if event_row else []
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    is_super_admin = admin_doc and admin_doc.role == "super_admin"
    is_event_team = _is_event_team_member(event, user_id)

    is_uhc = admin_doc and admin_doc.department == "uhc"
    is_dept_admin = admin_doc and admin_doc.department == event.get("event_type")

    if not (is_super_admin or is_event_team or is_uhc or is_dept_admin):
        raise HTTPException(status_code=403, detail="Not authorized to view participation details")

    result = []
    for ev_reg, p in registration_rows:
        prof = p.profile or {}

        # UHC filtering logic
        if is_uhc and not is_super_admin and not is_event_team:
            email = admin_doc.email or ""
            admin_house = email.split("-")[0].lower() if "-" in email else None
            # `(... or "")` rather than `.get("house", "")`: the default only applies
            # to a *missing* key, so a profile storing an explicit `house: None` — the
            # ordinary state of an account that has not completed registration — used
            # to reach `.lower()` on None and answer 500 for the whole roster, because
            # of one incomplete participant.
            participant_house = (p.house or prof.get("house") or "").lower()
            if participant_house != admin_house:
                continue

        result.append({
            "participant_id": p.participant_id,
            "name": p.full_name or prof.get("full_name"),
            "email": p.email,
            "phone": p.phone or prof.get("phone"),
            "house": p.house or prof.get("house"),
            "team_id": ev_reg.team_id,
            "team_role": (ev_reg.attributes or {}).get("team_role"),
        })

    event_team_details = []
    for membership, staff, participant in team_rows:
        attrs = staff.attributes or {}
        member_name = (participant.full_name if participant else None) or attrs.get("designation", "Admin")
        member_phone = (participant.phone if participant else None) or "Unknown"
        event_team_details.append({
            "user_id": str(staff.paradox_id),
            "role": membership.role,
            "name": member_name,
            "phone": member_phone
        })

    response_data = {
        "count": len(result),
        "participants": result,
        "event_team": event_team_details
    }

    if not is_uhc:
        response_data["total_daily_scans"] = _unique_attendance_today(event)

    return response_data


@router.post("/{event_id}/allocate_teams")
def allocate_teams(event_id: str, current_user: dict = Depends(get_current_staff)):
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        log_denied(
            current_user, "ALLOCATE_EVENT_TEAMS_DENIED", event_id,
            reason="event_not_found", details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Event not found")

    if not _is_event_head(event, user_id):
        log_denied(
            current_user, "ALLOCATE_EVENT_TEAMS_DENIED", event_id,
            reason="not_event_head", details={"status": 403},
        )
        raise HTTPException(status_code=403, detail="Only Event Heads are authorized to allocate teams")

    team_rules = event.get("team", {})
    min_size = team_rules.get("min", 1)
    max_size = team_rules.get("max", 1)

    if max_size <= 1:
        # A 200 that did nothing, and unaudited. An Event Head who runs allocation on
        # a solo event gets "Not a team event" and no record that they tried — and if
        # `team.max` is missing from the document entirely it defaults to 1 and lands
        # here too, which looks the same but is a data problem.
        log_config.info(
            _log,
            f"team allocation skipped for {event_id}: not a team event",
            {
                "event_id": event_id,
                "reason": "not_a_team_event",
                "team_max": team_rules.get("max"),
                "team_rules_present": bool(team_rules),
            },
        )
        return {"message": "Not a team event"}

    with SessionLocal.begin() as session:
        event_locked = session.scalar(select(Event).where(Event.id == event_row.id).with_for_update())
        registrations = session.execute(
            select(EventRegistration, Participant)
            .join(Participant, Participant.id == EventRegistration.participant_id)
            .where(EventRegistration.event_id == event_locked.id)
            .order_by(Participant.participant_id)
            .with_for_update(of=EventRegistration)
        ).all()
        solo_players = [(registration, participant) for registration, participant in registrations if not registration.team_id]
        random.shuffle(solo_players)
        teams_created = 0
        unteamed: List[Optional[str]] = []
        dropped_chunks: List[dict] = []
        log_config.info(_log, f"event team allocation starting for {event_id}", {
            "event_id": event_id,
            "registered": len(registrations),
            "unteamed_candidates": len(solo_players),
            "team_min": min_size,
            "team_max": max_size,
            "house_vs_house": bool(team_rules.get("house_vs_house_event", False)),
        })

        groups: list[tuple[str | None, list[tuple[EventRegistration, Participant]]]] = []
        if team_rules.get("house_vs_house_event", False):
            by_house: dict[str, list] = {}
            for pair in solo_players:
                registration, participant = pair
                house = participant.house or (participant.profile or {}).get("house") or "Unknown"
                by_house.setdefault(house, []).append(pair)
            groups = list(by_house.items())
        else:
            groups = [(None, solo_players)]

        for house, players in groups:
            for offset in range(0, len(players), max_size):
                chunk = players[offset:offset + max_size]
                if len(chunk) < min_size:
                    unteamed.extend(participant.participant_id for _, participant in chunk)
                    dropped_chunks.append({"house": house, "size": len(chunk), "min_required": min_size})
                    continue
                team_id = generator.next_team_id(_stored_event_type(event))
                team = ParticipantTeam(
                    event_id=event_locked.id,
                    team_id=team_id,
                    leader_id=chunk[0][1].id,
                    attributes={"house": house} if house else {},
                )
                session.add(team)
                session.flush()
                for registration, participant in chunk:
                    registration.team_id = team_id
                    registration.attributes = {**(registration.attributes or {}), "team_role": "member"}
                    session.add(ParticipantTeamMember(team_id=team.id, participant_id=participant.id, role="member"))
                teams_created += 1
        candidate_count = len(solo_players)

    for participant_id in unteamed:
        log_denied(
            current_user,
            "EVENT_TEAM_ALLOCATION_SKIPPED",
            participant_id,
            reason="group_below_minimum_size",
            details={"event_id": event_id, "team_min": min_size, "team_max": max_size},
        )

    log_batch(
        current_user,
        "ALLOCATE_EVENT_TEAMS",
        event_id,
        {
            # Unchanged and still first, for existing readers.
            "teams_created": teams_created,
            "candidates": len(solo_players),
            "teamed_count": len(solo_players) - len(unteamed),
            "unteamed_count": len(unteamed),
            "unteamed": [pid for pid in unteamed if pid],
            "dropped_groups": dropped_chunks,
            "team_min": min_size,
            "team_max": max_size,
        },
    )
    if unteamed:
        log_config.warning(
            _log,
            f"event team allocation left {len(unteamed)} of {len(solo_players)} candidate(s) without a team",
            {
                "event_id": event_id,
                "reason": "groups_below_minimum_size",
                "unteamed_count": len(unteamed),
                "dropped_groups": dropped_chunks,
            },
        )
    return {"message": f"Allocated {teams_created} teams"}


@router.post("/{event_id}/scan")
def scan_event_participant(event_id: str, request: ScanQRRequest, current_user: dict = Depends(get_current_staff)):
    """
    Admit one participant at an event gate.

    This route had the largest blind spot in the system. A scan of somebody not
    registered for the event returned **200** with `is_participating: false` and
    wrote nothing at all — no log row, no audit row — so a refused entry was
    indistinguishable from no scan ever having happened. It was also the only scan
    endpoint in the codebase with no `log_audit` call of any kind, successful or
    otherwise.

    The response is deliberately unchanged, including the 200 for a non-participant:
    the gate app decides what to show from `is_participating`, and changing the
    status code would break it. What changes is that all three outcomes — admitted,
    refused, and already-scanned — now leave a record.
    """
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        log_denied(
            current_user, "EVENT_SCAN_DENIED", event_id,
            reason="event_not_found", details={"scan_domain": "event"},
        )
        raise HTTPException(status_code=404, detail="Event not found")

    if not _is_event_team_member(event, user_id):
        log_denied(
            current_user, "EVENT_SCAN_DENIED", event_id,
            reason="not_on_event_team",
            details={"scan_domain": "event", "team_size": len(event.get("event_team") or [])},
        )
        raise HTTPException(status_code=403, detail="Not authorized to scan for this event")

    target_user, _ = verify_qr(request, actor=current_user, domain="event", target_id=event_id)

    participant_uuid = uuid.UUID(str(target_user["_id"]))
    with SessionLocal() as session:
        registration_exists = session.scalar(select(EventRegistration.id).where(
            EventRegistration.event_id == event_row.id,
            EventRegistration.participant_id == participant_uuid,
        )) is not None
    is_participating = registration_exists

    # Log successful scan
    if is_participating:
        now = datetime.now(timezone.utc)
        day = now.date()
        day_str = day.isoformat()
        scanner_uuid = uuid.UUID(str(current_user["_id"]))
        with SessionLocal.begin() as session:
            session.scalar(select(Participant.id).where(Participant.id == participant_uuid).with_for_update())
            already_logged = session.scalar(select(EventAttendance).where(
                EventAttendance.event_id == event_row.id,
                EventAttendance.participant_id == participant_uuid,
                EventAttendance.scanner_id == scanner_uuid,
                EventAttendance.service_date == day,
                EventAttendance.scan_type == "entry",
            ).with_for_update())
            if not already_logged:
                session.add(EventAttendance(
                    event_id=event_row.id,
                    event_identifier=event_row.event_id,
                    participant_id=participant_uuid,
                    scanner_id=scanner_uuid,
                    service_date=day,
                    scan_type="entry",
                    scanned_at=now,
                    details={"day": day_str},
                ))
        if not already_logged:
            log_scan(
                current_user, "event", "EVENT_SCAN", OUTCOME_ALLOWED,
                participant_id=target_user.get("participant_id"),
                target_id=event_id,
                details={"day": day_str, "event_oid": str(event["_id"])},
            )
        else:
            # A repeat scan by the same volunteer on the same day. The dedupe made
            # this a silent no-op returning 200, so a queue being scanned twice and
            # a queue being scanned once looked identical. Recorded as `duplicate`,
            # not `denied` — the participant is admitted either way — and it is what
            # explains why a volunteer's own `my_daily_scans` tally is lower than the
            # number of people they physically scanned.
            log_scan(
                current_user, "event", "EVENT_SCAN_DUPLICATE", OUTCOME_DUPLICATE,
                participant_id=target_user.get("participant_id"),
                target_id=event_id,
                reason="already_scanned_today_by_this_scanner",
                details={"day": day_str, "first_scanned_at": already_logged.scanned_at},
            )
    else:
        # The refusal that left no trace whatsoever. A student turned away from an
        # event they believe they registered for now produces a row naming them, the
        # gate, the volunteer, and how many events they *are* registered for — which
        # is what separates "never registered" from "registered for a different
        # event" from "deregistered without realising".
        log_scan(
            current_user, "event", "EVENT_SCAN_UNREGISTERED", OUTCOME_DENIED,
            participant_id=target_user.get("participant_id"),
            target_id=event_id,
            reason="not_registered_for_event",
            details={
                "event_oid": str(event["_id"]),
                "registrations_held": len(target_user.get("events") or []),
                # 200 is still returned; this makes clear in the trail that the gate
                # app, not this endpoint, decided whether to let them in.
                "http_status": 200,
            },
        )

    return {
        "name": target_user.get("profile", {}).get("full_name"),
        "email": target_user.get("email"),
        "is_participating": is_participating
    }


@router.get("/{event_id}/my_daily_scans")
def my_daily_scans(event_id: str, current_user: dict = Depends(get_current_staff)):
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    if not _is_event_team_member(event, user_id):
        raise HTTPException(status_code=403, detail="Not authorized")

    with SessionLocal() as session:
        scanner_id = session.scalar(select(StaffUser.id).where(StaffUser.paradox_id == user_id))
        count = session.scalar(select(func.count()).select_from(EventAttendance).where(
            EventAttendance.event_id == event_row.id,
            EventAttendance.scanner_id == scanner_id,
            EventAttendance.service_date == datetime.now(timezone.utc).date(),
        )) or 0

    return {"daily_unique_scans": count}


@router.get("/{event_id}/logs")
def event_logs(event_id: str, current_user: dict = Depends(get_current_staff)):
    """
    Every attendance scan recorded for this event.

    These rows have always been written by ``POST /events/{event_id}/scan`` but
    nothing read them back: participation only reported *today's* count, and
    ``my_daily_scans`` only the caller's own. That left the per-event attendance
    history unreachable, which is what the dashboard's event log view needs.

    Mirrors ``GET /workshops/{workshop_id}/logs`` — same super-admin gate, same
    ``{"logs": [...]}`` envelope — so the two read identically on the client.

    Attendance rows reference the event UUID, so this endpoint joins them to
    the readable event and participant records.
    """
    _require_super_admin(current_user)

    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
        scan_rows = session.execute(
            select(EventAttendance, Participant, StaffUser)
            .join(Participant, Participant.id == EventAttendance.participant_id)
            .outerjoin(StaffUser, StaffUser.id == EventAttendance.scanner_id)
            .where(EventAttendance.event_id == event_row.id)
            .order_by(EventAttendance.scanned_at.desc())
        ).all() if event_row else []
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    logs = [{
        "event_id": str(event_row.id),
        "participant_id": participant.participant_id,
        "scanned_by": staff.paradox_id if staff else None,
        "day": (attendance.details or {}).get("day"),
        "timestamp": attendance.scanned_at,
    } for attendance, participant, staff in scan_rows]
    log_audit(current_user, "READ_EVENT_LOGS", event_id, {"returned": len(logs)})
    return {"logs": logs}


class TeamUpdateInput(BaseModel):
    """
    A partial update: a head may move a participant without restating their role,
    or change the role without restating the team.

    Both fields default to ``None``, so "absent" and "explicitly null" look the
    same on the model — the route separates them with ``model_fields_set``
    instead. Only ``team_id`` is nullable in the data: clearing it takes the
    participant off their team, whereas a stored ``team_role`` is always one of
    `models.PARTICIPANT_TEAM_ROLES` — "leader" or "member".
    """
    team_id: Optional[str] = None
    team_role: Optional[Literal["leader", "member"]] = None


@router.put("/{event_id}/participant_teams/{participant_id}")
def update_participant_team(event_id: str, participant_id: str, payload: TeamUpdateInput, current_user: dict = Depends(get_current_staff)):
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
        participant_row = session.scalar(select(Participant).where(Participant.participant_id == participant_id)) if event_row else None
        registration_row = session.scalar(select(EventRegistration).where(
            EventRegistration.event_id == event_row.id,
            EventRegistration.participant_id == participant_row.id,
        )) if event_row and participant_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    if not _is_event_head(event, user_id):
        log_denied(
            current_user, "UPDATE_EVENT_TEAM_DENIED", event_id,
            reason="not_event_head",
            details={"participant_id": participant_id, "status": 403},
        )
        raise HTTPException(status_code=403, detail="Only Event Heads are authorized to modify participant teams")

    if participant_row is None or registration_row is None:
        log_denied(
            current_user, "UPDATE_EVENT_TEAM_DENIED", event_id,
            reason="participant_not_registered",
            details={"participant_id": participant_id},
        )
        raise HTTPException(status_code=404, detail="Participant not registered for this event")

    previous = {
        "team_id": registration_row.team_id,
        "team_role": (registration_row.attributes or {}).get("team_role", "member"),
    }

    def refuse(reason: str, detail: str) -> HTTPException:
        log_denied(
            current_user, "UPDATE_EVENT_TEAM_DENIED", event_id,
            reason=reason,
            details={"participant_id": participant_id, "status": 400},
        )
        return HTTPException(status_code=400, detail=detail)

    # An empty body used to be an accepted request that wrote null over both
    # fields, quietly dropping the participant out of their team — the caller
    # asked for nothing and got a mutation. Absent fields are now left alone,
    # and a request that names neither is refused rather than guessed at.
    supplied = payload.model_fields_set
    if not supplied:
        raise refuse("no_fields_supplied", "Provide team_id or team_role to update")

    clearing_team = "team_id" in supplied and payload.team_id is None

    team_id = payload.team_id if "team_id" in supplied else previous.get("team_id")
    if "team_role" in supplied:
        team_role = payload.team_role
    elif clearing_team:
        # Off any team, they hold the role a solo registrant holds.
        team_role = "member"
    else:
        team_role = previous.get("team_role")
    # No stored registration has a null role, so neither may a moved one.
    if team_role is None:
        team_role = "member"
    if team_id is None and team_role == "leader":
        raise refuse(
            "leader_without_team",
            "A participant with no team cannot hold the team leader role",
        )

    # `team.max` is the event's own rule about how many may compete together;
    # registration enforces it (`_resolve_registration_team`) and a hand-move
    # has to as well, or a head can seat a team that the event's rules forbid.
    # Counted excluding this participant, so re-stating someone's existing team
    # is measured against the same ceiling as moving them in for the first time.
    if team_id is not None:
        team_rules = event.get("team") or {}
        max_size = team_rules.get("max", 1)
        if max_size <= 1:
            raise refuse(
                "event_has_no_teams",
                "This event does not support team registration",
            )
        with SessionLocal() as session:
            others = session.scalar(select(func.count()).select_from(EventRegistration).where(
                EventRegistration.event_id == event_row.id,
                EventRegistration.team_id == team_id,
                EventRegistration.participant_id != participant_row.id,
            )) or 0
        if others + 1 > max_size:
            raise refuse("team_full", "This team is already full")

    with SessionLocal.begin() as session:
        event_locked = session.scalar(select(Event).where(Event.id == event_row.id).with_for_update())
        participant = session.scalar(select(Participant).where(Participant.id == participant_row.id).with_for_update())
        registration = session.scalar(select(EventRegistration).where(
            EventRegistration.event_id == event_locked.id,
            EventRegistration.participant_id == participant.id,
        ).with_for_update())
        if registration is None:
            raise HTTPException(status_code=404, detail="Participant not registered for this event")
        if team_id is not None:
            others = session.scalar(select(func.count()).select_from(EventRegistration).where(
                EventRegistration.event_id == event_locked.id,
                EventRegistration.team_id == team_id,
                EventRegistration.participant_id != participant.id,
            )) or 0
            if others + 1 > (event.get("team") or {}).get("max", 1):
                raise refuse("team_full", "This team is already full")
            team = session.scalar(select(ParticipantTeam).where(
                ParticipantTeam.event_id == event_locked.id,
                ParticipantTeam.team_id == team_id,
            ).with_for_update())
            if team is None:
                team = ParticipantTeam(event_id=event_locked.id, team_id=team_id, leader_id=participant.id)
                session.add(team)
                session.flush()
        old_team_id = registration.team_id
        if old_team_id and old_team_id != team_id:
            _remove_participant_team_link(session, event_locked.id, old_team_id, participant.id)
        registration.team_id = team_id
        registration.team_identifier = team_id
        registration.attributes = {**(registration.attributes or {}), "team_role": team_role}
        if team_id is not None:
            team = session.scalar(select(ParticipantTeam).where(
                ParticipantTeam.event_id == event_locked.id,
                ParticipantTeam.team_id == team_id,
            ).with_for_update())
            member = session.get(ParticipantTeamMember, (team.id, participant.id))
            if member is None:
                session.add(ParticipantTeamMember(team_id=team.id, participant_id=participant.id, role=team_role))
            else:
                member.role = team_role
            if team_role == "leader":
                team.leader_id = participant.id
    # Previously unaudited: an Event Head could move anybody between teams, or clear
    # their team entirely, with no trace. In a house-versus-house event that is the
    # difference between a result standing and being contested, so the row carries
    # both the old and the new team.
    log_audit(current_user, "UPDATE_EVENT_TEAM", event_id, {
        "participant_id": participant_id,
        "team_id": team_id,
        "team_role": team_role,
        "previous_team_id": previous.get("team_id"),
        "previous_team_role": previous.get("team_role"),
        "team_cleared": clearing_team,
    })
    return {"message": "Participant team updated"}


# ── announcements ────────────────────────────────────────────────────────────

def _is_registered_for(event: dict, current_user: dict) -> bool:
    if "participant_id" not in current_user:
        return False
    return any(
        str(ev.get("event_id")) == str(event["_id"])
        for ev in current_user.get("events", [])
    )


def _may_read_announcements(event: dict, current_user: dict) -> bool:
    if _is_super_admin(current_user):
        return True
    paradox_id = current_user.get("paradox_id")
    if paradox_id and _is_event_team_member(event, paradox_id):
        return True
    return _is_registered_for(event, current_user)


@router.post("/{event_id}/announcements")
def create_announcement(
    event_id: str,
    request: AnnouncementCreateRequest,
    current_user: dict = Depends(get_current_staff),
):
    """
    Publish a notification about this event to everybody registered for it.

    Restricted to the event's own Event Head, or a Super Admin — a volunteer
    or member on the team can scan and view participation, but broadcasting to
    every registrant is a head-of-event decision.

    Appended to `event.announcements`; delivered to participants via
    `GET /{event_id}/announcements` (poll) or `GET /{event_id}/announcements/stream`
    (SSE) — see below.
    """
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    if not (_is_super_admin(current_user) or _is_event_head(event, user_id)):
        raise HTTPException(status_code=403, detail="Only the Event Head can post announcements for this event")

    announcement = {
        "announcement_id": f"ANN{uuid.uuid4().hex[:12].upper()}",
        "message": request.message,
        "priority": request.priority,
        "created_by": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with SessionLocal.begin() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id).with_for_update())
        attrs = dict(event_row.attributes or {})
        announcements = list(attrs.get("announcements") or [])
        announcements.append(announcement)
        attrs["announcements"] = announcements
        event_row.attributes = attrs
        event_row.updated_at = datetime.now(timezone.utc)
    log_audit(current_user, "CREATE_ANNOUNCEMENT", event_id, {"priority": request.priority})
    return {"message": "Announcement published", "announcement": announcement}


@router.get("/{event_id}/announcements")
def list_announcements(event_id: str, current_user: dict = Depends(get_current_user)):
    """
    Newest first. Readable by whoever the announcement is *for* — a registered
    participant — plus whoever might have sent it: the event's own team, or a
    Super Admin.
    """
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    if not _may_read_announcements(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to read this event's announcements")

    announcements = sorted(
        event.get("announcements", []),
        key=lambda a: str(a.get("created_at") or ""),
        reverse=True,
    )
    return announcements


def _serialise_announcement(announcement: dict) -> dict:
    body = dict(announcement)
    created_at = body.get("created_at")
    if isinstance(created_at, datetime):
        body["created_at"] = created_at.isoformat() + "Z"
    return body


async def _announcement_stream(event_id: str, event_oid, request: Request):
    """
    Poll the event document for announcements newer than what the client has
    already seen, and emit each one as an SSE frame.

    Implemented as DB polling rather than an in-memory pub/sub queue on
    purpose: an in-memory queue only reaches subscribers connected to the same
    process, which breaks the moment this API runs with more than one uvicorn
    worker (the default for any real deployment). Polling the document is
    slightly higher latency but correct regardless of worker count, and it
    needs no new infrastructure (no Redis, no message broker) beyond what this
    project already has.

    Resumable via the standard `Last-Event-ID` header: a reconnecting client
    (whether via a retrying `fetch` loop or a library that honours SSE's
    reconnection contract) is not shown announcements it already has.
    """
    event_uuid = uuid.UUID(str(event_oid))
    last_seen_id = request.headers.get("last-event-id")
    seen_ids: set = set()

    if last_seen_id:
        # Seed `seen_ids` with every announcement up to and including the one
        # the client already has, so a reconnect does not replay history.
        #
        # Only when that id is genuinely in this event's history. The loop that did
        # this walked the array marking each announcement seen and stopped when it
        # reached the client's id — so an id that matched *nothing* marked the whole
        # history seen and the client was shown nothing at all, for as long as it
        # kept resuming with that id. Two ordinary things produce a stale id: the
        # announcement it names has since been removed, or the client reconnected
        # against a different event. Both used to end in silence, which is the worst
        # possible failure for a channel whose entire job is telling participants
        # where to be.
        #
        # An unknown id is therefore treated as a fresh connection: everything is
        # delivered. Showing an announcement twice is a visible, recoverable
        # annoyance; not showing it is neither.
        with SessionLocal() as session:
            current = session.get(Event, event_uuid)
            history = [ann.get("announcement_id") for ann in ((current.attributes or {}).get("announcements") or [])] if current else []
        if last_seen_id in history:
            seen_ids.update(history[: history.index(last_seen_id) + 1])
        else:
            log_config.info(
                _log,
                f"resuming announcement stream for {event_id} from an unknown Last-Event-ID; "
                "delivering the full history",
                {
                    "event_id": event_id,
                    "last_event_id": str(last_seen_id),
                    "reason": "unknown_last_event_id",
                    "announcements_held": len(history),
                },
            )

    heartbeat_every = 15  # seconds
    poll_every = 3  # seconds
    elapsed_since_heartbeat = 0.0

    try:
        while True:
            if await request.is_disconnected():
                break

            with SessionLocal() as session:
                current = session.get(Event, event_uuid)
                announcements = list((current.attributes or {}).get("announcements") or []) if current else []
            new_ones = [a for a in announcements if a.get("announcement_id") not in seen_ids]
            new_ones.sort(key=lambda a: str(a.get("created_at") or ""))

            for ann in new_ones:
                seen_ids.add(ann.get("announcement_id"))
                payload = json.dumps(_serialise_announcement(ann))
                yield f"id: {ann.get('announcement_id')}\nevent: announcement\ndata: {payload}\n\n"
                elapsed_since_heartbeat = 0.0

            await asyncio.sleep(poll_every)
            elapsed_since_heartbeat += poll_every
            if elapsed_since_heartbeat >= heartbeat_every:
                yield ": heartbeat\n\n"
                elapsed_since_heartbeat = 0.0
    except asyncio.CancelledError:
        # Client disconnected mid-sleep; exit quietly rather than propagating.
        return


@router.get("/{event_id}/announcements/stream")
async def stream_announcements(event_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """
    Live announcements for this event, as `text/event-stream`.

    Authenticated exactly like every other route in this API — an
    `Authorization: Bearer` header via `get_current_user` — rather than a
    `?token=` query parameter. The browser's native `EventSource` cannot send
    a header, so a browser client must instead open this with `fetch` and read
    `response.body` as a stream (or a small helper library such as
    `@microsoft/fetch-event-source` that does that parsing). This keeps the
    token out of the URL — and therefore out of server access logs, browser
    history, and any `Referer` header — matching every other endpoint here.

    Access is the same as the polling list above: the event's registered
    participants, its team, or a Super Admin.
    """
    with SessionLocal() as session:
        event_row = session.scalar(select(Event).where(Event.event_id == event_id))
        event = _event_record(session, event_row) if event_row else None
    if event_row is None:
        raise HTTPException(status_code=404, detail="Event not found")

    if not _may_read_announcements(event, current_user):
        raise HTTPException(status_code=403, detail="Not authorized to read this event's announcements")

    return StreamingResponse(
        _announcement_stream(event_id, event["_id"], request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx response buffering, if ever fronted by one
        },
    )
