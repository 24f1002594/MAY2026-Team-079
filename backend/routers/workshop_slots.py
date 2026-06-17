"""
Workshop slots — the D1S1, D2S2, ... time blocks workshops are scheduled
against.

Created and edited independently of any workshop by a Super Admin. A workshop
references a slot by `slot_id` and denormalizes that slot's `start_time` onto
its own document at creation time (see `routers.workshops.create_workshop`),
so the slot-clash check in `register_for_workshop` and the scan-window guard
in `_assert_scan_window` never have to join back to this collection on every
read.

Because the time is denormalized rather than looked up live, this file is
also where that denormalization is kept honest:

  * `PUT /workshop-slots/{slot_id}` pushes an edited `start_time` onto every
    workshop currently referencing that slot — otherwise an edited slot and
    the workshops "in" it would silently disagree about when they run.
  * `DELETE /workshop-slots/{slot_id}` removes every workshop referencing it
    (and their participants' bookings), rather than leaving orphaned
    workshops pointed at a slot that no longer exists.

Document shape:

    { slot_id, start_time, end_time, created_by, created_at, updated_at }
"""
from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime, timezone
from sqlalchemy import delete, select, func
from sqlalchemy.exc import IntegrityError

import log_config
from logger import log_audit, log_denied
from models import WorkshopSlotCreateRequest, WorkshopSlotUpdateRequest, parse_instant_utc
from db import SessionLocal
from schema import StaffUser, Workshop, WorkshopAttendance, WorkshopRegistration, WorkshopSlot
from dependencies import get_current_staff

router = APIRouter(prefix="/workshop-slots", tags=["Workshop Slots"])

_log = log_config.get_logger("paradox.workshop_slots")


def _is_super_admin(current_user: dict) -> bool:
    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        return bool(session.scalar(
            select(StaffUser.id).where(StaffUser.paradox_id == user_id, StaffUser.role == "super_admin")
        ))


def _require_super_admin(current_user: dict) -> None:
    if not _is_super_admin(current_user):
        log_denied(
            current_user,
            "AUTHZ_DENIED",
            None,
            reason="not_super_admin",
            details={"resource": "workshop_slots", "status": 403},
        )
        raise HTTPException(status_code=403, detail="Only Super Admins can perform this action")


@router.post("")
def create_workshop_slot(request: WorkshopSlotCreateRequest, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user)

    new_slot = WorkshopSlot(
        slot_id=request.slot_id,
        start_time=parse_instant_utc(request.start_time, "start_time").replace(tzinfo=timezone.utc),
        end_time=parse_instant_utc(request.end_time, "end_time").replace(tzinfo=timezone.utc),
        attributes={
            "created_by": current_user["_id"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    try:
        with SessionLocal.begin() as session:
            session.add(new_slot)
    except IntegrityError as exc:
        log_denied(
            current_user, "CREATE_WORKSHOP_SLOT_DENIED", request.slot_id,
            reason="slot_id_already_exists", details={"status": 400},
        )
        raise HTTPException(status_code=400, detail="A slot with this slot_id already exists") from exc
    log_audit(current_user, "CREATE_WORKSHOP_SLOT", request.slot_id)
    return {"message": "Workshop slot created"}


@router.get("")
def list_workshop_slots():
    """
    The slot catalogue — no token required. Unlike a workshop, a slot carries
    no participant, staff, or bookkeeping data; it is calendar metadata that a
    create-workshop form needs to read before a user has signed in.
    """
    with SessionLocal() as session:
        rows = session.scalars(select(WorkshopSlot).order_by(WorkshopSlot.slot_id)).all()
        return [
            {
                "slot_id": row.slot_id,
                "start_time": row.start_time.isoformat().replace("+00:00", "Z"),
                "end_time": row.end_time.isoformat().replace("+00:00", "Z"),
                "created_at": row.attributes.get("created_at"),
                "updated_at": row.attributes.get("updated_at"),
            }
            for row in rows
        ]


@router.put("/{slot_id}")
def update_workshop_slot(slot_id: str, request: WorkshopSlotUpdateRequest, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user)

    update_data = {k: v for k, v in request.model_dump().items() if v is not None}
    if not update_data:
        log_denied(
            current_user, "UPDATE_WORKSHOP_SLOT_DENIED", slot_id,
            reason="nothing_to_update", details={"status": 400}, audit=False,
        )
        raise HTTPException(status_code=400, detail="Nothing to update")

    # Merge onto the stored document before validating end > start, the same
    # pattern events.py uses for RegistrationWindowUpdate: a request that only
    # changes one bound must still be checked against the other bound as it
    # already stands, not treated as trivially valid on its own.
    with SessionLocal() as session:
        slot = session.scalar(select(WorkshopSlot).where(WorkshopSlot.slot_id == slot_id))
        slot_snapshot = None if slot is None else {
            "id": slot.id,
            "start_time": slot.start_time,
            "end_time": slot.end_time,
            "attributes": dict(slot.attributes or {}),
        }
    if slot_snapshot is None:
        log_denied(
            current_user, "UPDATE_WORKSHOP_SLOT_DENIED", slot_id,
            reason="slot_not_found", details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Workshop slot not found")

    new_start = update_data.get("start_time", slot_snapshot["start_time"].isoformat())
    new_end = update_data.get("end_time", slot_snapshot["end_time"].isoformat())
    start_dt = parse_instant_utc(new_start, "start_time")
    end_dt = parse_instant_utc(new_end, "end_time")
    if end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end_time must be after start_time")

    start_value = parse_instant_utc(new_start, "start_time").replace(tzinfo=timezone.utc)
    end_value = parse_instant_utc(new_end, "end_time").replace(tzinfo=timezone.utc)
    updated_at = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        slot = session.get(WorkshopSlot, slot_snapshot["id"])
        if slot is None:
            raise HTTPException(status_code=404, detail="Workshop slot not found")
        slot.start_time = start_value
        slot.end_time = end_value
        slot.attributes = {**(slot.attributes or {}), "updated_at": updated_at.isoformat()}
        workshops = session.scalars(select(Workshop).where(Workshop.slot_id == slot.id)).all()
        affected = 0
        if "start_time" in update_data:
            for workshop in workshops:
                workshop.start_time = start_value
                workshop.updated_at = updated_at
                affected += 1

    # Cascade: every workshop referencing this slot must agree with it about
    # when it runs. Only start_time is denormalized onto a workshop (that is
    # what the scan-window guard and slot-clash check read), so only it is
    # pushed here, and only when this request actually changed it.
    log_audit(current_user, "UPDATE_WORKSHOP_SLOT", slot_id, {
        "workshops_updated": affected,
        # Editing a slot's `start_time` moves the scan window of every workshop in it.
        # A volunteer who was scanning happily a minute ago starts getting "Scanning
        # window not yet open", and the cause is this request rather than anything at
        # their desk. Both values go in so the shift is legible.
        "previous_start_time": slot_snapshot["start_time"].isoformat() if "start_time" in update_data else None,
        "new_start_time": update_data.get("start_time"),
        "fields_updated": sorted(k for k in update_data if k != "updated_at"),
    })
    if affected:
        log_config.warning(
            _log,
            f"slot {slot_id} start_time changed, shifting the scan window of {affected} workshop(s)",
            {
                "slot_id": slot_id,
                "reason": "slot_time_cascade",
                "workshops_updated": affected,
                "previous_start_time": slot_snapshot["start_time"].isoformat(),
                "new_start_time": update_data.get("start_time"),
            },
        )
    return {"message": "Workshop slot updated", "workshops_updated": affected}


@router.delete("/{slot_id}")
def delete_workshop_slot(slot_id: str, current_user: dict = Depends(get_current_staff)):
    """
    Deletes the slot, and every workshop scheduled against it — a Super
    Admin decision, not a soft-orphan: a workshop with no slot has no time and
    no way to be scheduled again, so it is removed rather than left dangling.

    Participants' bookings for each removed workshop are pulled the same way
    `delete_workshop` already does for a single workshop, so no participant is
    left holding a reference to a workshop that no longer exists.
    """
    _require_super_admin(current_user)

    with SessionLocal() as session:
        slot = session.scalar(select(WorkshopSlot).where(WorkshopSlot.slot_id == slot_id))
        if slot is None:
            slot_info = None
            affected_workshops = []
        else:
            slot_info = (slot.id, slot.start_time)
            affected_workshops = session.scalars(select(Workshop).where(Workshop.slot_id == slot.id)).all()
            affected_workshops = [
                {
                    "_id": str(workshop.id),
                    "workshop_id": workshop.workshop_id,
                    "name": workshop.name,
                    "registration_count": workshop.attributes.get("registration_count"),
                    "participant_count": workshop.attributes.get("participant_count"),
                }
                for workshop in affected_workshops
            ]
    if slot_info is None:
        log_denied(
            current_user, "DELETE_WORKSHOP_SLOT_DENIED", slot_id,
            reason="slot_not_found", details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Workshop slot not found")

    # The widest-reaching delete in the API: one request removes a slot, every
    # workshop in it, and every participant's booking for those workshops. Announced
    # before it runs, with the scale stated, so the trail shows the intent as well as
    # the individual results.
    log_config.warning(
        _log,
        f"deleting slot {slot_id} will remove {len(affected_workshops)} workshop(s) and their bookings",
        {
            "slot_id": slot_id,
            "reason": "slot_delete_cascade",
            "workshops": [w.get("workshop_id") for w in affected_workshops],
            "destructive": True,
        },
    )

    registrations_by_workshop: dict[str, int] = {}
    with SessionLocal.begin() as session:
        slot = session.get(WorkshopSlot, slot_info[0])
        workshops = session.scalars(select(Workshop).where(Workshop.slot_id == slot_info[0])).all()
        workshop_ids = [workshop.id for workshop in workshops]
        bookings_removed = 0
        if workshop_ids:
            counts = session.execute(
                select(WorkshopRegistration.workshop_id, func.count())
                .where(WorkshopRegistration.workshop_id.in_(workshop_ids))
                .group_by(WorkshopRegistration.workshop_id)
            ).all()
            registrations_by_workshop = {str(workshop_id): count for workshop_id, count in counts}
            bookings_removed = sum(registrations_by_workshop.values())
            session.execute(delete(WorkshopAttendance).where(WorkshopAttendance.workshop_id.in_(workshop_ids)))
            session.execute(delete(WorkshopRegistration).where(WorkshopRegistration.workshop_id.in_(workshop_ids)))
            session.execute(delete(Workshop).where(Workshop.id.in_(workshop_ids)))
        session.execute(delete(WorkshopSlot).where(WorkshopSlot.id == slot_info[0]))

    for workshop in affected_workshops:
        log_audit(current_user, "DELETE_WORKSHOP", workshop.get("workshop_id"), {
            "reason": "slot_deleted", "slot_id": slot_id,
            "name": workshop.get("name"),
            "registration_count": workshop.get("registration_count"),
            "participant_count": workshop.get("participant_count"),
            "bookings_removed": registrations_by_workshop.get(workshop["_id"], 0),
        })
    log_audit(current_user, "DELETE_WORKSHOP_SLOT", slot_id, {
        "workshops_deleted": len(affected_workshops),
        "workshop_ids": [w.get("workshop_id") for w in affected_workshops],
        "bookings_removed": bookings_removed,
        "start_time": slot_info[1].isoformat(),
    })

    return {"message": "Workshop slot deleted", "workshops_deleted": len(affected_workshops)}
