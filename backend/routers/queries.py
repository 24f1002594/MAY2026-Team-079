"""
Participant queries — Epic 6 (raise · track · assign · answer) and Story 5.4.

Why this is a collection of its own
-----------------------------------
A query needs a participant to write free text that a *different* user reads
back. Nothing else in this API does that. Every participant-writable field is
either returned only to its own author (``events[].registration_data``, via
``GET /events/my_registrations``) or is load-bearing data a query would corrupt
(``profile.*`` is every roster's identity, ``team_id`` is what
``allocate_teams`` reads, ``accommodation.registered`` is a one-bit flag that
cancels a hostel request). So the channel had to be new.

How a query reaches a team
--------------------------
There is no per-category routing. Every query, regardless of ``category``
(``hostel`` | ``mess`` | ``event`` | ``workshop`` | ``general``), is visible to
and answerable by the same flat **query resolution team** — a roster a Super
Admin builds in ``query_team_collection`` (see the ``/queries/team`` routes
below), independent of the ``hostel_team``/``mess_team``/``event_team``/
``workshop_team`` arrays those entities carry for their own purposes.
``category`` and ``target_id`` remain on the query purely as labels: they say
what the query is about and are still validated against a real entity at
write time, but they no longer decide who can read or answer it.

A query team member sees, and may self-claim (``PATCH`` with
``assigned_to`` set to their own id), the entire queue — resolved queries stay
in it too, so the roster keeps a shared history rather than losing a query the
moment it closes. Filtering that down to "still open" is what the ``status``
query param on ``GET /queries`` is for.

What is deliberately not in a query document
--------------------------------------------
No email, no phone. Denormalising contact details onto a row every query team
member can fetch would widen disclosure well past what answering a question
needs. The reply thread is the channel back, so the team never needs a number.
``participant_name`` and ``participant_house`` are stored because a thread
addressed to nobody in particular is unanswerable.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from datetime import datetime, timezone
from typing import Optional
import uuid
from sqlalchemy import select

from logger import log_audit, log_denied
from db import SessionLocal
from schema import Event, Hostel, Mess, Participant, QueryTeamMember, StaffUser, SupportQuery, Workshop
from dependencies import get_current_user, get_current_staff, get_current_participant
from models import (
    PAGE_LIMIT_MAX,
    QueryCreateRequest,
    QueryUpdateRequest,
    QueryReplyRequest,
    QueryTeamAssignRequest,
)

router = APIRouter(prefix="/queries", tags=["Queries"])

# category -> (collection, readable id field) — used only to validate that a
# query's target_id names a real entity at write time. No team field is read
# off these collections any more; who may handle a query is entirely a
# question of query_team_collection / super_admin now.
CATEGORY_ROUTING = {
    "hostel": (Hostel, "hostel_id"),
    "mess": (Mess, "mess_id"),
    "event": (Event, "event_id"),
    "workshop": (Workshop, "workshop_id"),
}

# `general` has no target entity, so it has no entity to validate against.
# Listed separately rather than folded into the map above, because a `general`
# query with a `target_id` is a category error, not a routing one.
CATEGORIES = set(CATEGORY_ROUTING) | {"general"}

STATUSES = {"open", "assigned", "resolved"}


def _is_super_admin(paradox_id: str) -> bool:
    with SessionLocal() as session:
        return session.scalar(select(StaffUser.id).where(
            StaffUser.paradox_id == paradox_id, StaffUser.role == "super_admin"
        )) is not None


def _is_query_team_member(paradox_id: str) -> bool:
    with SessionLocal() as session:
        return session.scalar(
            select(QueryTeamMember.staff_id)
            .join(StaffUser, QueryTeamMember.staff_id == StaffUser.id)
            .where(StaffUser.paradox_id == paradox_id)
        ) is not None


def _query_record(session, row: SupportQuery) -> dict:
    participant = session.get(Participant, row.author_id)
    attributes = row.attributes or {}
    assigned_to = attributes.get("assigned_to")
    if assigned_to is None and row.assignee_id:
        staff = session.get(StaffUser, row.assignee_id)
        assigned_to = staff.paradox_id if staff else None
    return {
        "query_id": row.query_id,
        "participant_id": participant.participant_id if participant else None,
        "participant_name": attributes.get("participant_name"),
        "participant_house": attributes.get("participant_house"),
        "category": row.category,
        "target_id": attributes.get("target_id"),
        "subject": attributes.get("subject", ""),
        "body": row.body,
        "status": row.status,
        "assigned_team": attributes.get("assigned_team"),
        "assigned_to": assigned_to,
        "replies": attributes.get("replies") or [],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "resolved_at": attributes.get("resolved_at"),
    }


def _require_query_access(current_user: dict) -> str:
    """
    Every staff-side query route needs the same thing: a Super Admin, or a
    member of the flat query resolution team. Anyone else is refused outright
    — there is no per-entity scope left to fall back to an empty result with.
    """
    paradox_id = current_user.get("paradox_id")
    if not (_is_super_admin(paradox_id) or _is_query_team_member(paradox_id)):
        # Queries carry participants' free text, which is the one place in this API a
        # student writes something a different user reads back. Refusing access to it
        # is worth a durable row.
        log_denied(
            current_user,
            "AUTHZ_DENIED",
            None,
            reason="not_on_query_team",
            details={"resource": "queries", "status": 403},
        )
        raise HTTPException(status_code=403, detail="Not authorized to access queries")
    return paradox_id


def _require_super_admin(current_user: dict) -> None:
    if not _is_super_admin(current_user.get("paradox_id")):
        log_denied(
            current_user,
            "AUTHZ_DENIED",
            None,
            reason="not_super_admin",
            details={"resource": "query_team", "status": 403},
        )
        raise HTTPException(status_code=403, detail="Only Super Admins can manage the query team")


def _public_view(query: dict) -> dict:
    """One shape for both sides of the thread, so neither can drift from the other."""
    return {k: v for k, v in query.items() if k != "_id"}


@router.post("")
def raise_query(request: QueryCreateRequest, current_user: dict = Depends(get_current_participant)):
    """
    Raise a query — Story 6.1, and the durable record Story 5.4 needs.

    The target entity is validated at write time rather than at read time: a
    query naming a block that does not exist would otherwise be accepted, routed
    to nobody, and sit unanswered looking exactly like a query nobody has got to
    yet.
    """
    category = request.category.lower().strip()
    if category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid category. Must be one of: {', '.join(sorted(CATEGORIES))}",
        )

    target_id = request.target_id
    target_entity_id = None
    if category == "general":
        # A general query has no owning entity, so naming one is a category error —
        # refused rather than quietly dropped. Discarding it silently answered 200
        # to a request the caller got wrong: somebody reporting a fault against a
        # specific block, under the wrong category, was told their query was raised
        # and lost the only thing that said which block they meant.
        if target_id:
            raise HTTPException(
                status_code=400,
                detail="A general query cannot name a target_id; choose the category "
                       "that owns it instead",
            )
        # Kept None so the scope filter cannot accidentally match it against a
        # team's target list.
        target_id = None
    else:
        if not target_id:
            raise HTTPException(status_code=400, detail=f"A {category} query must name a {category}")
        model, id_field = CATEGORY_ROUTING[category]
        with SessionLocal() as session:
            target_entity_id = session.scalar(select(model.id).where(getattr(model, id_field) == target_id))
        if target_entity_id is None:
            raise HTTPException(status_code=404, detail=f"No {category} found with id {target_id}")

    profile = current_user.get("profile") or {}
    now = datetime.now(timezone.utc)
    query_row = SupportQuery(
        query_id=f"QRY{now.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6].upper()}",
        author_id=uuid.UUID(str(current_user["_id"])),
        category=category,
        target_hostel_id=target_entity_id if category == "hostel" else None,
        target_mess_id=target_entity_id if category == "mess" else None,
        target_event_id=target_entity_id if category == "event" else None,
        target_workshop_id=target_entity_id if category == "workshop" else None,
        body=request.body.strip(),
        status="open",
        attributes={
            "participant_name": profile.get("full_name"),
            "participant_house": profile.get("house"),
            "target_id": target_id,
            "subject": request.subject.strip(),
            "assigned_team": None,
            "assigned_to": None,
            "replies": [],
            "resolved_at": None,
        },
        created_at=now,
        updated_at=now,
    )
    with SessionLocal.begin() as session:
        session.add(query_row)
        session.flush()
        query_doc = _query_record(session, query_row)
    log_audit(
        current_user,
        "RAISE_QUERY",
        query_doc["query_id"],
        {"category": category, "target_id": target_id},
    )
    return {"message": "Query raised", "query_id": query_doc["query_id"], "query": _public_view(query_doc)}


@router.get("/mine")
def my_queries(current_user: dict = Depends(get_current_participant)):
    """
    Track your own queries — Story 6.2. Newest first, replies included, so the
    participant sees the status *and* what was said about it.

    Declared before ``/{query_id}`` so the literal path is not captured as an id.
    """
    with SessionLocal() as session:
        rows = session.scalars(
            select(SupportQuery)
            .join(Participant, SupportQuery.author_id == Participant.id)
            .where(Participant.participant_id == current_user["participant_id"])
            .order_by(SupportQuery.created_at.desc())
        ).all()
        return [_query_record(session, row) for row in rows]


@router.get("")
def list_queries(
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(100, ge=1, le=PAGE_LIMIT_MAX),
    current_user: dict = Depends(get_current_staff),
):
    """
    The shared queue — Story 6.3, and the source for the operational
    dashboard's open-queries and hostel-issues panels (Story 9.1).

    Not scoped by entity any more: a Super Admin and every member of the query
    resolution team see the same unrestricted queue, across every category
    including ``general``, resolved queries included — a query team member can
    self-claim any of it by ``PATCH``-ing ``assigned_to`` to their own id. A
    staff member who is on neither gets a 403; there is no per-entity scope
    left to fall back to an empty result with.

    ``status`` and ``category`` filter server-side for the same reason
    ``/audit-logs`` does: ``limit`` applies before any client-side filter could.
    """
    _require_query_access(current_user)

    if status is not None and status not in STATUSES:
        raise HTTPException(
            status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(STATUSES))}"
        )

    with SessionLocal() as session:
        query = select(SupportQuery).order_by(SupportQuery.created_at.desc()).limit(limit)
        if status is not None:
            query = query.where(SupportQuery.status == status)
        if category is not None:
            query = query.where(SupportQuery.category == category)
        return [_query_record(session, row) for row in session.scalars(query).all()]


@router.patch("/{query_id}")
def update_query(query_id: str, request: QueryUpdateRequest, current_user: dict = Depends(get_current_staff)):
    """
    Set status and assignment — Story 6.3.

    Only the fields a request carries are written, so a screen that reassigns a
    query cannot blank its status on the way past. A query team member sets
    ``assigned_to`` to their own id to self-claim a query; a Super Admin may
    assign to anyone.
    """
    _require_query_access(current_user)
    with SessionLocal() as session:
        row = session.scalar(select(SupportQuery).where(SupportQuery.query_id == query_id))
        query = _query_record(session, row) if row else None
    if not query:
        raise HTTPException(status_code=404, detail="Query not found")

    # `model_fields_set` separates "left out of the request" from "sent as null" —
    # the same distinction `PATCH /profile/complete` relies on. Guarding on
    # `is not None` alone conflated the two, which made an assignment impossible to
    # clear: a query handed to somebody who then left the fest stayed assigned to
    # them forever, because the only way to express "nobody" was a null the route
    # read as "unchanged".
    sent = request.model_fields_set

    update: dict = {}
    if request.status is not None:
        if request.status not in STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Must be one of: {', '.join(sorted(STATUSES))}",
            )
        update["status"] = request.status
        # Stamped when the work finishes, and left alone otherwise.
        #
        # It used to be overwritten with None on *any* non-resolved status, so
        # resolve -> reopen -> resolve destroyed the first resolution time — and that
        # field is the only record of when the work was actually done, which is what
        # "how long did that take" is answered from. A resolved_at on a reopened query
        # is not misleading: `status` already says it is open again, and the pair
        # together are the history.
        if request.status == "resolved":
            update["resolved_at"] = datetime.now(timezone.utc).isoformat()

    if "assigned_team" in sent:
        update["assigned_team"] = request.assigned_team
    if "assigned_to" in sent:
        update["assigned_to"] = request.assigned_to
        # Handing a query to somebody is what "assigned" means. An explicit
        # status in the same request still wins, so resolve-and-assign works.
        # Releasing it (an explicit null) implies nothing — the status is whatever
        # the caller says it is, or whatever it already was.
        if request.assigned_to is not None:
            update.setdefault("status", "assigned")

    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")

    fields_updated = sorted(key for key in update if key != "updated_at")
    update["updated_at"] = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        row = session.scalar(select(SupportQuery).where(SupportQuery.query_id == query_id).with_for_update())
        if row is None:
            raise HTTPException(status_code=404, detail="Query not found")
        if "assigned_to" in update:
            assigned_to = update["assigned_to"]
            if assigned_to is None:
                row.assignee_id = None
            else:
                assignee = session.scalar(select(StaffUser).where(StaffUser.paradox_id == assigned_to))
                if assignee is None:
                    raise HTTPException(status_code=404, detail="Assigned staff member not found")
                row.assignee_id = assignee.id
        if "status" in update:
            row.status = update.pop("status")
        if "updated_at" in update:
            row.updated_at = update.pop("updated_at")
        row.attributes = {**(row.attributes or {}), **update}
        updated_query = _query_record(session, row)
    log_audit(current_user, "UPDATE_QUERY", query_id, {"fields_updated": fields_updated})
    return {"message": "Query updated", "query": _public_view(updated_query)}


@router.post("/{query_id}/replies")
def reply_to_query(query_id: str, request: QueryReplyRequest, current_user: dict = Depends(get_current_user)):
    """
    The conversation — Story 6.4, and the half of 6.2 that makes a status useful.

    Takes either token type: the participant who raised it and a query team
    member write to the same thread. Nobody else can read or write it, which is
    checked per-role rather than per-token, so a staff member off the query
    team is refused even though their token is valid.

    Neither refusal discloses whether the id exists. The route used to look the
    query up and 404 first, so any authenticated user could tell a real query id
    from an invented one — and these threads hold participants' free text, the one
    place in this API where a student writes something another user reads back.
    The two sides get there differently, because their refusals are different
    kinds of thing:

      * a staff member off the query team may handle *no* query, so which one was
        asked for never enters into it. Refused before the lookup, exactly as
        `update_query` and every other staff-side route here already does.
      * a participant may only handle their *own*, which cannot be decided without
        the document. So "not yours" and "not there" collapse into the same 404:
        for a participant, no other query exists — the same thing `GET
        /queries/mine` says by omitting it from the list.
    """
    is_staff = "paradox_id" in current_user

    if is_staff:
        author_id = current_user["paradox_id"]
        if not (_is_super_admin(author_id) or _is_query_team_member(author_id)):
            log_denied(
                current_user, "REPLY_QUERY_DENIED", query_id,
                reason="not_on_query_team", details={"status": 403},
            )
            raise HTTPException(status_code=403, detail="Not authorized to handle this query")
        # `designation` stays first on purpose — a participant reading the thread
        # should see the desk that answered ("Query Desk"), not the individual.
        # `name` is the new middle rung: the chain used to fall straight from
        # designation to `role`, so a named account with no designation signed its
        # replies "super_admin". A raw role is not a person and is not a desk; it is
        # an internal enum value, and it was the only thing the participant saw.
        author_name = (
            current_user.get("designation")
            or current_user.get("name")
            or current_user.get("role")
            or "Fest team"
        )
        author_type = "staff"

    with SessionLocal() as session:
        query_row = session.scalar(select(SupportQuery).where(SupportQuery.query_id == query_id))
        query = _query_record(session, query_row) if query_row else None

    if not is_staff:
        author_id = current_user["participant_id"]
        if not query or query.get("participant_id") != author_id:
            # A participant reaching for a thread that is not theirs. Recorded because
            # the threads contain other students' words, so an attempt to read or
            # append to one is worth being able to review afterwards — and the row
            # keeps the distinction the response withholds.
            log_denied(
                current_user, "REPLY_QUERY_DENIED", query_id,
                reason="query_not_found" if not query else "not_query_author",
                details={"status": 404, "owner_participant_id": (query or {}).get("participant_id")},
            )
            raise HTTPException(status_code=404, detail="Query not found")
        author_name = (current_user.get("profile") or {}).get("full_name") or "Participant"
        author_type = "participant"

    if not query:
        log_denied(
            current_user, "REPLY_QUERY_DENIED", query_id,
            reason="query_not_found", details={"status": 404}, audit=False,
        )
        raise HTTPException(status_code=404, detail="Query not found")

    now = datetime.now(timezone.utc)
    reply = {
        "author_id": author_id,
        "author_type": author_type,
        "author_name": author_name,
        "body": request.body.strip(),
        "timestamp": now.isoformat(),
    }
    with SessionLocal.begin() as session:
        query_row = session.scalar(select(SupportQuery).where(SupportQuery.query_id == query_id).with_for_update())
        if query_row is None:
            raise HTTPException(status_code=404, detail="Query not found")
        attributes = dict(query_row.attributes or {})
        attributes["replies"] = [*(attributes.get("replies") or []), reply]
        query_row.attributes = attributes
        query_row.updated_at = now
    log_audit(current_user, "REPLY_QUERY", query_id, {"author_type": author_type})
    return {"message": "Reply added", "reply": reply}


# ── query resolution team roster ────────────────────────────────────────────
#
# A flat, Super-Admin-managed list of who may see and handle every query,
# independent of category. It is modeled as a relational join table so
# membership can be queried by staff user id.


def _team_member_view(row: dict) -> dict:
    """The roster row, denormalized with a display name where one is on file —
    the same shape `events.py` hands back for `event_team` details."""
    with SessionLocal() as session:
        staff = session.scalar(select(StaffUser).where(StaffUser.paradox_id == row["user_id"]))
    attributes = staff.attributes or {} if staff else {}
    return {
        "user_id": row["user_id"],
        "name": attributes.get("name") or attributes.get("designation"),
        "designation": attributes.get("designation"),
        "department": staff.department if staff else None,
        "added_at": row.get("added_at"),
        "added_by": row.get("added_by"),
    }


@router.post("/team")
def add_query_team_member(request: QueryTeamAssignRequest, current_user: dict = Depends(get_current_staff)):
    """
    Add a staff member to the query resolution team — Super Admin only.

    `user_id` must already exist in `backend_teams_collection`; this roster
    grants query access on top of an existing staff account, it does not
    create one.
    """
    _require_super_admin(current_user)

    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        staff = session.scalar(select(StaffUser).where(StaffUser.paradox_id == request.user_id))
        if staff is None:
            raise HTTPException(status_code=404, detail="user_id must reference an existing backend_teams member")
        if session.get(QueryTeamMember, staff.id):
            raise HTTPException(status_code=400, detail="This staff member is already on the query team")
        added_by = session.scalar(select(StaffUser).where(StaffUser.paradox_id == current_user.get("paradox_id")))
        member = QueryTeamMember(staff_id=staff.id, added_by_id=added_by.id if added_by else None, added_at=now)
        session.add(member)
        row = {"user_id": request.user_id, "added_at": now, "added_by": current_user.get("paradox_id")}
    log_audit(current_user, "ASSIGN_QUERY_TEAM", request.user_id, {})
    return {"message": "Added to query team", "member": _team_member_view(row)}


@router.get("/team")
def list_query_team(current_user: dict = Depends(get_current_staff)):
    """The current roster — Super Admin only, same as `event_team` details are staff-gated."""
    _require_super_admin(current_user)
    with SessionLocal() as session:
        rows = session.execute(
            select(StaffUser.paradox_id, QueryTeamMember.added_at, QueryTeamMember.added_by_id)
            .join(QueryTeamMember, QueryTeamMember.staff_id == StaffUser.id)
            .order_by(QueryTeamMember.added_at.asc())
        ).all()
        members = []
        for paradox_id, added_at, added_by_id in rows:
            added_by = session.get(StaffUser, added_by_id) if added_by_id else None
            members.append({
                "user_id": paradox_id,
                "added_at": added_at,
                "added_by": added_by.paradox_id if added_by else None,
            })
    return [_team_member_view(row) for row in members]


@router.delete("/team/{user_id}")
def remove_query_team_member(user_id: str, current_user: dict = Depends(get_current_staff)):
    """Frees this person from query duty. Does not touch anything they already replied to or were assigned."""
    _require_super_admin(current_user)

    with SessionLocal.begin() as session:
        staff = session.scalar(select(StaffUser).where(StaffUser.paradox_id == user_id))
        if staff is None or session.get(QueryTeamMember, staff.id) is None:
            raise HTTPException(status_code=404, detail="user_id is not on the query team")
        session.delete(session.get(QueryTeamMember, staff.id))
    log_audit(current_user, "REMOVE_QUERY_TEAM_MEMBER", user_id, {})
    return {"message": "Removed from query team"}
