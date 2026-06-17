"""
Participant-reported hostel and mess faults — Story 5.4.

Why this is a new collection rather than a field on something that already
exists: the story needs a participant to write free text that a *different*
user — the block's or hall's own duty team — reads back, and then needs that
team to write a status the participant can read. Every participant-writable
field in the rest of the API fails one half or the other. ``registration_data``
is returned only to its own author. ``team_id`` is the event's team data, so
carrying reports in it corrupts ``allocate_teams``. ``profile.*`` is identity,
read on every roster. So there was no channel, and this file is the channel.

The shape of the guards is borrowed rather than invented. Filing is
``get_current_participant`` and is restricted to the facility the caller is
actually placed in, which is the same fact ``GET /hostels/my_hostel`` and
``GET /mess/my_mess`` already report to them. Reading and answering is
``get_current_staff`` narrowed to the teams that name the caller — the identical
membership test ``scan_hostel``, ``scan_mess`` and ``update_mess_menu`` already
make — with a Super Admin override so the fest-wide view has somewhere to come
from.

Additive throughout: no existing route, model, guard or response field is
touched by this module.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from logger import log_audit, log_denied
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
import random
import uuid
from sqlalchemy import and_, func, or_, select

from db import SessionLocal
from schema import (
    FacilityIssue, Hostel, HostelStaff, Mess, MessStaff, Participant,
    StaffUser,
)
from dependencies import get_current_staff, get_current_participant
from models import PAGE_LIMIT_MAX

router = APIRouter(prefix="/issues", tags=["Issues"])

# The two facility kinds a report can name. Kept as a set rather than an enum so
# the check reads the same way as the category check below it.
FACILITY_TYPES = {"hostel", "mess"}

# Categories are validated rather than free text because the answering team's
# console groups and labels by them: an unrecognised category would be a row that
# no filter finds. The two lists are deliberately different — a mess has no
# broken furniture and a block has no dietary complaint.
CATEGORIES = {
    "hostel": {
        "water",
        "electricity",
        "cleanliness",
        "furniture",
        "internet",
        "safety",
        "noise",
        "other",
    },
    "mess": {
        "food_quality",
        "hygiene",
        "service",
        "timing",
        "dietary",
        "other",
    },
}

# The lifecycle a report moves through. `open` on filing, `in_progress` while the
# team works it, `resolved` when it is done — and only `resolved` frees a slot
# against the cap below.
STATUSES = {"open", "in_progress", "resolved"}

# A ceiling on unresolved reports per participant per facility. Not a rate limit
# on filing — a guard against one participant burying a block's queue under
# duplicates of the same broken shower.
MAX_OPEN_PER_FACILITY = 10


class IssueCreateRequest(BaseModel):
    facility_type: str = Field(..., description="hostel | mess")
    facility_id: str = Field(
        ...,
        description="A readable hostel_id or mess_id. Must be the facility this participant is actually placed in.",
    )
    category: str = Field(
        ...,
        description="For a hostel — water | electricity | cleanliness | furniture | internet | safety | noise | other. For a mess — food_quality | hygiene | service | timing | dietary | other. Validated, because the answering team's console groups and labels by it.",
    )
    subject: str = Field(..., min_length=3, max_length=120)
    body: str = Field(..., min_length=3, max_length=2000)
    room: Optional[str] = Field(
        None,
        description="Where the fault is. Defaults to the room the participant is allotted, so the common case needs no typing.",
    )


class IssueUpdateRequest(BaseModel):
    status: Optional[str] = Field(None, description="open | in_progress | resolved")
    note: Optional[str] = Field(
        None,
        max_length=2000,
        description='A line the reporter will read. Valid on its own, with no status change — "we have ordered the part" is worth saying.',
    )


def _new_issue_id() -> str:
    """
    A readable id, in the same spirit as `paradox_id`'s `BT{timestamp}`.

    The random suffix is what `paradox_id` lacks: two participants filing in the
    same second would otherwise collide, and two reports sharing an id is a
    report that cannot be answered.
    """
    return f"ISS{int(datetime.now(timezone.utc).timestamp())}{random.randint(1000, 9999)}"


def _is_super_admin(user_id: str) -> bool:
    """paradox_id"""
    with SessionLocal() as session:
        return session.scalar(select(StaffUser.id).where(
            StaffUser.paradox_id == user_id, StaffUser.role == "super_admin"
        )) is not None


def _duty_facilities(user_id: str) -> dict:
    """
    The blocks and halls whose team names this staff member.

    Same membership test as `scan_hostel` / `scan_mess` / `update_mess_menu`,
    and deliberately *not* gated on the `logging` flag: that flag governs whether
    somebody may work a turnstile, which has nothing to do with whether they
    should see a broken shower reported on their own block.
    """
    with SessionLocal() as session:
        staff_id = session.scalar(select(StaffUser.id).where(StaffUser.paradox_id == user_id))
        if staff_id is None:
            return {"hostel": [], "mess": []}
        hostels = session.scalars(
            select(Hostel.hostel_id)
            .join(HostelStaff, HostelStaff.hostel_id == Hostel.id)
            .where(HostelStaff.staff_id == staff_id)
        ).all()
        messes = session.scalars(
            select(Mess.mess_id)
            .join(MessStaff, MessStaff.mess_id == Mess.id)
            .where(MessStaff.staff_id == staff_id)
        ).all()
        return {"hostel": list(hostels), "mess": list(messes)}


def _resolve_facility(facility_type: str, facility_id: str) -> dict:
    """The facility document, or a 404. Never a silent no-op."""
    model = Hostel if facility_type == "hostel" else Mess
    key = "hostel_id" if facility_type == "hostel" else "mess_id"
    with SessionLocal() as session:
        row = session.scalar(select(model).where(getattr(model, key) == facility_id))
        facility = {"_id": str(row.id), key: getattr(row, key)} if row else None

    if not facility:
        raise HTTPException(status_code=404, detail=f"{facility_type.capitalize()} not found")
    return facility


def _placement_error(participant: dict, facility_type: str, facility: dict) -> Optional[str]:
    """
    Whether this participant is actually placed in this facility.

    A report is about somewhere the reporter is staying or eating; without this
    check any participant could file against any block, and the duty team's list
    would stop being a list of their own residents' problems. The two halves of
    the check are asymmetric because the two collections store the link
    differently: `accommodation.hostel_id` keeps the readable id, while
    mess allocations keep a foreign key to the hall row and expose its readable id.
    """
    if facility_type == "hostel":
        if participant.get("accommodation", {}).get("hostel_id") != facility["hostel_id"]:
            return "You are not allotted to this hostel"
        return None

    if participant.get("mess", {}).get("mess_id") != facility["_id"]:
        return "You are not allotted to this mess"
    return None


def _issue_record(session, row: FacilityIssue) -> dict:
    reporter = session.get(Participant, row.reporter_id)
    attributes = row.attributes or {}
    return {
        "issue_id": row.issue_id,
        "participant_id": reporter.participant_id if reporter else None,
        "facility_type": row.facility_type,
        "facility_id": row.facility_id,
        "category": row.category,
        "subject": attributes.get("subject"),
        "body": row.body,
        "room": attributes.get("room"),
        "status": row.status,
        "updates": attributes.get("updates") or [],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _public_issue(issue: dict) -> dict:
    """
    One report as its author should see it.

    `updates` is included on purpose — it is the whole of what makes the story
    *trackable* rather than a write-only suggestion box. `by` is dropped from
    each update: which volunteer typed a note is staff bookkeeping, and the
    audit trail keeps it.
    """
    return {
        "issue_id": issue.get("issue_id"),
        "facility_type": issue.get("facility_type"),
        "facility_id": issue.get("facility_id"),
        "category": issue.get("category"),
        "subject": issue.get("subject"),
        "body": issue.get("body"),
        "room": issue.get("room"),
        "status": issue.get("status"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "updates": [
            {"at": u.get("at"), "status": u.get("status"), "note": u.get("note")}
            for u in issue.get("updates", [])
        ],
    }


def _staff_issue(session, issue: dict) -> dict:
    """
    One report as the answering team should see it.

    Adds the reporter, because a team that cannot call the person back cannot
    resolve anything. Name and phone come from the same `profile` fields
    `hostel_statistics` and `mess_statistics` already hand to staff, so this
    discloses nothing those routes do not.
    """
    reporter = session.scalar(select(Participant).where(Participant.participant_id == issue.get("participant_id")))

    return {
        **_public_issue(issue),
        "reporter": {
            "participant_id": issue.get("participant_id"),
            "name": reporter.full_name if reporter else None,
            "phone": reporter.phone if reporter else None,
            "room": issue.get("room") or None,
        },
        "updates": [
            {
                "at": u.get("at"),
                "status": u.get("status"),
                "note": u.get("note"),
                "by": u.get("by"),
            }
            for u in issue.get("updates", [])
        ],
    }


@router.post("")
def report_issue(
    request: IssueCreateRequest,
    current_user: dict = Depends(get_current_participant),
):
    """
    File a hostel or mess fault — Story 5.4. Participants only, and only against
    the facility they are actually placed in: a hostel report requires
    accommodation.hostel_id to match, a mess report requires mess.mess_id to
    match, otherwise 403. The facility must exist (404 otherwise) and the
    category must be one the facility type has (400 otherwise). A participant may
    hold at most 10 unresolved reports per facility; resolving one frees a slot.
    Status starts at open. Audited as ISSUE_REPORT against the facility id.

    Declared before `/{issue_id}` routes so nothing literal is captured as an id.
    """
    facility_type = request.facility_type.strip().lower()
    if facility_type not in FACILITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail="facility_type must be 'hostel' or 'mess'",
        )

    category = request.category.strip().lower()
    if category not in CATEGORIES[facility_type]:
        allowed = ", ".join(sorted(CATEGORIES[facility_type]))
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {allowed}",
        )

    facility = _resolve_facility(facility_type, request.facility_id)

    placement = _placement_error(current_user, facility_type, facility)
    if placement:
        # Somebody reporting a fault in a facility they are not placed in. Often a
        # genuine confusion about which block or hall they were allotted, which is
        # itself worth seeing — a run of these against one facility suggests the
        # allocation people were told does not match the one on record.
        log_denied(
            current_user, "ISSUE_REPORT_DENIED", request.facility_id,
            reason="not_placed_in_facility",
            details={"facility_type": facility_type, "category": category, "status": 403},
        )
        raise HTTPException(status_code=403, detail=placement)

    with SessionLocal() as session:
        outstanding = session.scalar(
            select(func.count())
            .select_from(FacilityIssue)
            .join(Participant, FacilityIssue.reporter_id == Participant.id)
            .where(
                Participant.participant_id == current_user["participant_id"],
                FacilityIssue.facility_type == facility_type,
                FacilityIssue.facility_id == request.facility_id,
                FacilityIssue.status != "resolved",
            )
        ) or 0
    if outstanding >= MAX_OPEN_PER_FACILITY:
        # A participant blocked from reporting a fault. Worth a durable row rather
        # than only a file line: hitting the cap means ten of their reports are
        # sitting unresolved, so this is as much a signal about the facility team's
        # backlog as about the reporter.
        log_denied(
            current_user, "ISSUE_REPORT_DENIED", request.facility_id,
            reason="open_report_cap_reached",
            details={
                "facility_type": facility_type,
                "category": category,
                "outstanding": outstanding,
                "cap": MAX_OPEN_PER_FACILITY,
            },
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"You already have {MAX_OPEN_PER_FACILITY} unresolved reports for this facility."
                " Wait for one to be resolved before filing another."
            ),
        )

    now = datetime.now(timezone.utc)
    issue_id = _new_issue_id()
    issue = FacilityIssue(
        issue_id=issue_id,
        reporter_id=uuid.UUID(str(current_user["_id"])),
        facility_type=facility_type,
        facility_id=request.facility_id,
        hostel_ref_id=uuid.UUID(facility["_id"]) if facility_type == "hostel" else None,
        mess_ref_id=uuid.UUID(facility["_id"]) if facility_type == "mess" else None,
        category=category,
        status="open",
        body=request.body.strip(),
        attributes={
            "subject": request.subject.strip(),
            # Falls back to the allotted room so the common case needs no typing.
            "room": (request.room or "").strip() or current_user.get("accommodation", {}).get("room"),
            "updates": [],
        },
        created_at=now,
        updated_at=now,
    )
    with SessionLocal.begin() as session:
        session.add(issue)
    log_audit(
        current_user,
        "ISSUE_REPORT",
        request.facility_id,
        {"issue_id": issue_id, "facility_type": facility_type, "category": category},
    )

    return {"message": "Issue reported", "issue_id": issue_id, "status": "open"}


@router.get("/mine")
def my_issues(current_user: dict = Depends(get_current_participant)):
    """
    Every report this participant has filed, newest first, each with the status
    history that makes the story trackable rather than a write-only suggestion
    box. Returns {count, issues}. An update's note is included; which volunteer
    wrote it is not — that is staff bookkeeping and the audit trail keeps it. No
    other reporter's details appear here.

    Declared before `/{issue_id}` so the literal path is not captured as an id.
    """
    with SessionLocal() as session:
        issues = session.scalars(
            select(FacilityIssue)
            .join(Participant, FacilityIssue.reporter_id == Participant.id)
            .where(Participant.participant_id == current_user["participant_id"])
            .order_by(FacilityIssue.created_at.desc())
        ).all()
        rows = [_public_issue(_issue_record(session, issue)) for issue in issues]
    return {"count": len(rows), "issues": rows}


@router.get("")
def list_issues(
    status: Optional[str] = None,
    facility_type: Optional[str] = None,
    facility_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=PAGE_LIMIT_MAX),
    current_user: dict = Depends(get_current_staff),
):
    """
    The reports this staff member is answerable for.

    A Super Admin sees every report in the fest; everybody else sees exactly the
    blocks and halls whose hostel_team or mess_team names them — the same
    membership test /hostels/{id}/scan and /mess/{id}/scan make, but deliberately
    not gated on the logging flag, since seeing a fault on your own block is not
    the same permission as working its turnstile. A staffer on no team gets an
    empty list, not a 403 — having no duty is not an authorization failure, and a
    console that errors at a volunteer between postings reads as a bug.

    Each row carries a reporter object with the participant's id, name, phone and
    room, so the team can call them back. status, facility_type and facility_id
    narrow the list; an unrecognised status is a 400 rather than a filter that
    silently matches nothing.
    """
    user_id = current_user.get("paradox_id")
    filters = []

    # What was asked for is judged before what this caller may see. The other order
    # ran the no-duty early return first, so `?status=pending` was a 400 for a Super
    # Admin and a 200 with an empty list for a staffer on no team — the same request
    # valid or invalid depending on who sent it. That hid the typo from exactly the
    # people most likely to make one: a volunteer between postings, reading an empty
    # queue as "no reports" rather than "you spelled the filter wrong".
    if status:
        if status not in STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of: {', '.join(sorted(STATUSES))}",
            )
        filters.append(FacilityIssue.status == status)
    if facility_type:
        filters.append(FacilityIssue.facility_type == facility_type)
    if facility_id:
        filters.append(FacilityIssue.facility_id == facility_id)

    if not _is_super_admin(user_id):
        duty = _duty_facilities(user_id)
        scopes = []
        if duty["hostel"]:
            scopes.append(and_(FacilityIssue.facility_type == "hostel", FacilityIssue.facility_id.in_(duty["hostel"])))
        if duty["mess"]:
            scopes.append(and_(FacilityIssue.facility_type == "mess", FacilityIssue.facility_id.in_(duty["mess"])))
        if not scopes:
            return {"count": 0, "issues": []}
        filters.append(or_(*scopes))

    with SessionLocal() as session:
        statement = select(FacilityIssue).where(*filters).order_by(FacilityIssue.created_at.desc()).limit(limit)
        issues = session.scalars(statement).all()
        rows = [_staff_issue(session, _issue_record(session, issue)) for issue in issues]
    return {"count": len(rows), "issues": rows}


@router.patch("/{issue_id}")
def update_issue(
    issue_id: str,
    request: IssueUpdateRequest,
    current_user: dict = Depends(get_current_staff),
):
    """
    Move a report along, and say something the reporter will read.

    Scoped identically to GET /issues — only a Super Admin or somebody on that
    facility's own team may answer for it, so the reporter cannot resolve their
    own report. Every call appends to `updates` rather than overwriting, so a
    participant sees the history rather than only the latest word. A note with no
    status change is valid; an empty body is a 400, as is a status outside
    open | in_progress | resolved. Audited as ISSUE_UPDATE against the facility
    id.

    The report is resolved and the caller authorised before the body is judged.
    The other order reported the body's shape for an issue that was not there —
    "Provide a status, a note, or both" against `ISS-NOPE` — which sends whoever
    is debugging it to look at their payload when the id is what is wrong. Same
    reason `update_participant_team` and `update_query` check existence first.
    """
    with SessionLocal() as session:
        issue_row = session.scalar(select(FacilityIssue).where(FacilityIssue.issue_id == issue_id))
        issue = _issue_record(session, issue_row) if issue_row else None
    if not issue:
        raise HTTPException(status_code=404, detail="Issue not found")

    user_id = current_user.get("paradox_id")
    if not _is_super_admin(user_id):
        duty = _duty_facilities(user_id)
        if issue["facility_id"] not in duty.get(issue["facility_type"], []):
            log_denied(
                current_user, "ISSUE_UPDATE_DENIED", issue_id,
                reason="not_on_facility_team",
                details={
                    "facility_type": issue.get("facility_type"),
                    "facility_id": issue.get("facility_id"),
                    "status": 403,
                },
            )
            raise HTTPException(
                status_code=403,
                detail="Not authorized to answer for this facility",
            )

    if request.status is None and (request.note is None or not request.note.strip()):
        raise HTTPException(status_code=400, detail="Provide a status, a note, or both")

    if request.status is not None and request.status not in STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(sorted(STATUSES))}",
        )

    now = datetime.now(timezone.utc)
    note = (request.note or "").strip() or None
    update_entry = {
        "at": now.isoformat(),
        "by": user_id,
        "status": request.status or issue.get("status"),
        "note": note,
    }

    with SessionLocal.begin() as session:
        issue_row = session.scalar(select(FacilityIssue).where(FacilityIssue.issue_id == issue_id).with_for_update())
        if issue_row is None:
            raise HTTPException(status_code=404, detail="Issue not found")
        if request.status is not None:
            issue_row.status = request.status
        issue_row.updated_at = now
        attributes = dict(issue_row.attributes or {})
        attributes["updates"] = [*(attributes.get("updates") or []), update_entry]
        issue_row.attributes = attributes

    log_audit(
        current_user,
        "ISSUE_UPDATE",
        issue["facility_id"],
        {"issue_id": issue_id, "status": update_entry["status"], "noted": note is not None},
    )

    return {"message": "Issue updated", "issue_id": issue_id, "status": update_entry["status"]}
