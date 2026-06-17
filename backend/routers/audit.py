from fastapi import APIRouter, HTTPException, Depends, Query
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set
from sqlalchemy import func, select

from db import SessionLocal
from schema import Participant, StaffUser, SystemLog
import log_config
from dependencies import get_current_staff
from logger import email_local_part
from models import PAGE_LIMIT_MAX

router = APIRouter(prefix="/audit-logs", tags=["Audit"])

_log = log_config.get_logger("paradox.audit.read")

# Keys inside `details` that hold a person's id rather than a value. Resolved to
# names alongside the actor, because a row like "assigned BT1755… as volunteer"
# names two different people and neither is readable as an id.
PERSON_DETAIL_KEYS = ("participant_id", "team_user_id", "assigned_user", "user_id")

# The meal sittings a `MESS_SCAN` can be filed under. A scan carrying anything
# else is counted but reported separately rather than discarded — see
# `_meal_summary`.
MEAL_SLOTS = ("breakfast", "lunch", "dinner")


def _naive_utc(value: str, field: str) -> datetime:
    """
    An ISO 8601 instant, in the form the trail is actually stored in.

    Entries are written as timezone-aware UTC datetimes. Comparing those against
    a naive bound raises in Python, and comparing them against a local-time bound shifts the
    window by the caller's offset — enough to file a whole evening's swipes under
    the wrong day. So an offset-bearing input is converted to UTC and then
    stripped, which is the only form that compares correctly against what is
    stored.
    """
    text = value.strip()
    # `fromisoformat` before 3.11 rejects a trailing `Z`, and callers send one.
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"`{field}` must be an ISO 8601 datetime, e.g. 2026-08-21T00:00:00Z",
        )
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _trail_query(
    target_id: Optional[str],
    action: Optional[str],
    since: Optional[str],
    until: Optional[str],
) -> list:
    """SQL conditions shared by the trail and summary.

    Built in one place precisely so the two cannot drift: a summary that counted a
    different set from the rows beside it would be worse than no summary at all.
    The window is half-open — `since` inclusive, `until` exclusive — so
    consecutive days tile without double-counting the midnight row.
    """
    conditions = []
    if target_id is not None:
        conditions.append(SystemLog.target_id == target_id)
    if action is not None:
        conditions.append(SystemLog.action == action)

    if since is not None:
        conditions.append(SystemLog.created_at >= _naive_utc(since, "since").replace(tzinfo=timezone.utc))
    if until is not None:
        conditions.append(SystemLog.created_at < _naive_utc(until, "until").replace(tzinfo=timezone.utc))

    return conditions


def _require_super_admin(current_user: dict) -> None:
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        is_admin = session.scalar(select(StaffUser.id).where(
            StaffUser.paradox_id == user_id, StaffUser.role == "super_admin"
        )) is not None
    if not is_admin:
        # Deliberately a file-only line rather than an audit row. An attempt to read
        # the audit trail must not be able to write to the audit trail: otherwise
        # anyone holding a staff token could inflate this collection at will, and the
        # rows they generated would be indistinguishable from real activity. The file
        # log records it instead, which is outside the reach of this endpoint.
        log_config.warning(
            _log,
            "audit log access refused",
            {
                "reason": "not_super_admin",
                "actor_id": user_id,
                "actor_role": current_user.get("role"),
                "resource": "audit_logs",
                "status": 403,
                "refusal": True,
            },
        )
        raise HTTPException(
            status_code=403, detail="Only Super Admins can view audit logs"
        )


def _meal_summary(session, conditions: list) -> Dict[str, Any]:
    """
    Meals served in a window, counted per diner rather than per swipe.

    `MESS_SCAN` records one row per successful scan, so counting rows answers "how
    many times was a card read", not "how many people were fed" — a double scan at
    a busy counter inflated the board's headline. Collapsing to one entry per
    `(diner, day, slot)` is what makes the figure a meal count, and the difference
    is reported as `duplicate_scans` rather than hidden.

    Rows whose `day` or `slot` is missing or unrecognised are still counted in
    `meals_served`; they are simply also reported as `unclassified` because there
    is no cell in the day × slot grid to draw them in. Dropping them, which is
    what the client used to do, made the headline quietly smaller than the trail
    it was derived from.
    """
    scans = session.scalar(
        select(func.count()).select_from(SystemLog).where(*conditions, SystemLog.action == "MESS_SCAN")
    ) or 0
    scan_rows = session.scalars(
        select(SystemLog).where(*conditions, SystemLog.action == "MESS_SCAN")
    ).all()

    by_slot: Counter = Counter()
    by_day: Counter = Counter()
    diners: Set[str] = set()
    meals_served = 0
    unclassified = 0

    distinct_meals = set()
    for row in scan_rows:
        details = row.details or {}
        participant = details.get("participant_id")
        day = details.get("day")
        slot = details.get("slot")
        distinct_meals.add((participant, day, slot))

    for participant, day_value, slot in distinct_meals:
        if isinstance(participant, str) and participant:
            diners.add(participant)

        meals_served += 1

        slot_ok = isinstance(slot, str) and slot in MEAL_SLOTS
        try:
            day = int(day_value)
            day_ok = True
        except (TypeError, ValueError):
            day, day_ok = 0, False

        if slot_ok and day_ok:
            by_slot[slot] += 1
            by_day[str(day)] += 1
        else:
            unclassified += 1

    return {
        "scans": scans,
        "meals_served": meals_served,
        "duplicate_scans": scans - meals_served,
        "unique_diners": len(diners),
        "unclassified": unclassified,
        "by_slot": {slot: by_slot.get(slot, 0) for slot in MEAL_SLOTS},
        "by_day": dict(sorted(by_day.items(), key=lambda item: int(item[0]))),
    }


def _iso_utc(value: Any) -> Any:
    """
    A timestamp the client can read unambiguously.

    Stored timestamps are UTC. Responses use an explicit UTC offset, so a browser
    never interprets a timestamp as local time. This also normalizes older rows.
    """
    if isinstance(value, datetime):
        aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def _display_names(session, ids: Iterable[Optional[str]]) -> Dict[str, str]:
    """
    Ids to readable names, in two queries for the whole page of results.

    Deliberately not one query per row: the trail is read 100-1000 entries at a
    time, and a lookup per entry would turn one request into hundreds. Two
    collections have to be consulted because `actor_id` spans both id namespaces
    — staff `paradox_id` and participant `participant_id` — with the older
    entries carrying nothing to say which.
    """
    wanted: Set[str] = {i for i in ids if i}
    if not wanted:
        return {}

    names: Dict[str, str] = {}

    staff_rows = session.scalars(select(StaffUser).where(StaffUser.paradox_id.in_(wanted))).all()
    for staff in staff_rows:
        attributes = staff.attributes or {}
        label = (
            attributes.get("name")
            or attributes.get("designation")
            or email_local_part(staff.email)
        )
        if label:
            names[staff.paradox_id] = label

    # Only the ids still unaccounted for, so a staff name is never overwritten by
    # the participant record of the same person.
    remaining = wanted - set(names)
    if remaining:
        for person in session.scalars(select(Participant).where(Participant.participant_id.in_(remaining))).all():
            label = person.full_name or (person.profile or {}).get("full_name") or email_local_part(person.email)
            if label:
                names[person.participant_id] = label

    return names


@router.get("/summary")
def audit_log_summary(
    target_id: Optional[str] = None,
    action: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    current_user: dict = Depends(get_current_staff),
):
    """
    Exact counts over the trail, with no row limit.

    `GET /audit-logs` takes a `limit`, which is right for a table but wrong for a
    total: the dashboard was reading `rows.length` off a capped page and labelling
    it "Recorded Actions", so a fest with more history than the cap silently
    reported the cap. Counting server-side is the only way for those figures to be
    totals rather than floors, and it costs one request instead of paging the
    whole trail into the browser.

    `actor_ids` is the distinct set, not a sample, which is what lets the caller
    answer "which staff acted in this window" exactly. It replaces intersecting
    the staff roster with whatever actors happened to appear in the newest N rows.

    `meals` is present only when the window could contain `MESS_SCAN` rows — that
    is, when `action` is unset or is `MESS_SCAN` — and is `null` otherwise, so a
    caller asking about hostel entries is not handed a meal count.
    """
    _require_super_admin(current_user)

    conditions = _trail_query(target_id, action, since, until)
    with SessionLocal() as session:
        total = session.scalar(
            select(func.count()).select_from(SystemLog).where(*conditions)
        ) or 0
        by_action = dict(session.execute(
            select(SystemLog.action, func.count())
            .where(*conditions)
            .group_by(SystemLog.action)
        ).all())
        actor_ids = sorted(
            actor for actor in session.scalars(
                select(SystemLog.actor_id).where(*conditions).distinct()
            ).all()
            if isinstance(actor, str) and actor
        )
        meals = None
        if action is None or action == "MESS_SCAN":
            meals = _meal_summary(session, _trail_query(target_id, "MESS_SCAN", since, until))

    return {
        "total": total,
        "by_action": dict(sorted(by_action.items())),
        "distinct_actors": len(actor_ids),
        "actor_ids": actor_ids,
        "meals": meals,
        "window": {"since": since, "until": until},
    }


@router.get("")
def view_audit_logs(
    limit: int = Query(100, ge=1, le=PAGE_LIMIT_MAX),
    target_id: Optional[str] = None,
    action: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    current_user: dict = Depends(get_current_staff)
):
    """
    The audit trail, newest first.

    ``target_id`` narrows the trail to one entity — an event, workshop, mess hall,
    or hostel block — which is what the dashboard's per-entity log view reads.
    Filtering server-side rather than pulling the whole trail and sifting it in the
    client matters here: ``limit`` would otherwise silently cut off an entity's
    older entries, because it applies before any client-side filter can run.

    ``action`` narrows to one kind of action, e.g. ``HOSTEL_ENTRY``. Both are
    optional, so the unfiltered call is unchanged.

    ``since`` and ``until`` are ISO 8601 instants bounding ``timestamp``, half-open
    so consecutive windows tile cleanly. They exist because "today" was previously
    approximated by fetching the newest N rows and filtering them in the browser,
    which makes any daily figure a floor the moment the fest produces more than N
    rows in a day. Filtering here removes the cap from the question entirely.
    ``GET /audit-logs/summary`` takes the same four filters and returns exact
    counts over them.

    Every entry carries a ``names`` map from each person id it mentions to that
    person's name, so the client can show "Priya Raman" where it used to show
    ``BT1755…`` without knowing which collection an id belongs to. Entries written
    since names began being recorded already carry ``actor_name``; the map is what
    makes the entries written before that readable too. ``actor_id`` and
    ``details`` are unchanged, so exports and filters that key on ids still work.
    """
    _require_super_admin(current_user)

    conditions = _trail_query(target_id, action, since, until)
    with SessionLocal() as session:
        stored_logs = session.scalars(
            select(SystemLog).where(*conditions).order_by(SystemLog.created_at.desc()).limit(limit)
        ).all()
        logs: List[Dict[str, Any]] = [
            {
                "timestamp": row.created_at,
                "actor_id": row.actor_id,
                "actor_name": row.actor_name,
                "actor_type": row.actor_type,
                "actor_role": row.actor_role,
                "action": row.action,
                "target_id": row.target_id,
                "details": row.details or {},
            }
            for row in stored_logs
        ]

    # One pass to collect every id the page mentions, one lookup, then attach.
    referenced: Set[str] = set()
    for log in logs:
        if log.get("actor_id"):
            referenced.add(log["actor_id"])
        # `target_id` is usually an entity — a hall, a block, an event — but for
        # UPDATE_PARTICIPANT it is a person, so it is offered to the lookup too.
        # An id that matches nobody simply does not come back.
        if log.get("target_id"):
            referenced.add(log["target_id"])
        details = log.get("details") or {}
        for key in PERSON_DETAIL_KEYS:
            value = details.get(key)
            if isinstance(value, str) and value:
                referenced.add(value)

    with SessionLocal() as session:
        names = _display_names(session, referenced)

    # Who read the trail, and with which filters — to the file log only, for the same
    # reason as the refusal above: recording audit reads *into* the audit collection
    # would let a Super Admin refreshing a dashboard bury the actions the trail
    # exists to show, and every read would generate a row that the next read
    # reports.
    log_config.info(
        _log,
        f"audit trail read: {len(logs)} row(s)",
        {
            "actor_id": current_user.get("paradox_id"),
            "returned": len(logs),
            "limit": limit,
            "filter_target_id": target_id,
            "filter_action": action,
            "filter_since": since,
            "filter_until": until,
        },
    )

    for log in logs:
        log["timestamp"] = _iso_utc(log.get("timestamp"))
        # Recorded at write time when available — that is the name as it was when
        # the action happened — and only otherwise resolved from the collections.
        if not log.get("actor_name"):
            log["actor_name"] = names.get(log.get("actor_id") or "")
        log.setdefault("actor_type", None)
        log.setdefault("actor_role", None)

        details = log.get("details") or {}
        mentioned: Dict[str, str] = {}
        for key in PERSON_DETAIL_KEYS:
            value = details.get(key)
            if isinstance(value, str) and value in names:
                mentioned[value] = names[value]
        target = log.get("target_id")
        if target in names:
            mentioned[target] = names[target]
        actor_id = log.get("actor_id")
        if actor_id and log.get("actor_name"):
            mentioned[actor_id] = log["actor_name"]
        log["names"] = mentioned

    return logs
