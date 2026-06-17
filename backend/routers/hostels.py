from fastapi import APIRouter, HTTPException, Depends
from logger import (
    OUTCOME_ALLOWED, OUTCOME_DENIED,
    log_audit, log_batch, log_denied, log_integrity, log_scan,
)
from datetime import datetime, timezone
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
import uuid

import log_config
from db import SessionLocal
from schema import AccommodationAllocation, Hostel, HostelRoom, HostelStaff, Participant, Payment, StaffUser
from dependencies import get_current_user, get_current_staff, get_current_participant, verify_qr
from models import ScanQRRequest, MockPaymentRequest
from id_generator import SequentialIDGenerator, generate_room_numbers
from payments import simulate_payment

generator = SequentialIDGenerator("HSTL")

router = APIRouter(prefix="/hostels", tags=["Hostels"])

_log = log_config.get_logger("paradox.hostels")


def _require_super_admin(current_user: dict, operation: str) -> str:
    """The Super Admin gate shared by this router's administrative routes."""
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
            details={"operation": operation, "resource": "hostels", "status": 403},
        )
        raise HTTPException(status_code=403, detail="Not authorized")
    return user_id


def _hostel_record(session, hostel: Hostel, *, expose_internal_id: bool = True) -> dict:
    rooms = session.scalars(
        select(HostelRoom).where(HostelRoom.hostel_id == hostel.id).order_by(HostelRoom.room_number)
    ).all()
    room_rows = []
    for room in rooms:
        occupants = session.scalars(
            select(Participant.participant_id)
            .join(AccommodationAllocation, AccommodationAllocation.participant_id == Participant.id)
            .where(AccommodationAllocation.room_id == room.id)
            .order_by(Participant.participant_id)
        ).all()
        room_rows.append({"room_number": room.room_number, "capacity": room.capacity, "occupants": list(occupants)})
    team = session.execute(
        select(HostelStaff, StaffUser).join(StaffUser, StaffUser.id == HostelStaff.staff_id)
        .where(HostelStaff.hostel_id == hostel.id).order_by(StaffUser.paradox_id)
    ).all()
    result = {
        "hostel_id": hostel.hostel_id,
        "name": hostel.name,
        "capacity": hostel.capacity,
        "gender": hostel.gender,
        "sharing": hostel.sharing,
        "current_occupancy": hostel.current_occupancy,
        "rooms": room_rows,
        "hostel_team": [
            {"user_id": staff.paradox_id, "role": membership.role, "attendance": membership.scanning_enabled}
            for membership, staff in team
        ],
        "created_at": hostel.created_at,
        "updated_at": hostel.updated_at,
    }
    if expose_internal_id:
        result["_id"] = str(hostel.id)
    return result

# The fixed hostel fee charged by the mock payment endpoint below. Never
# accepted from the client — see `MockPaymentRequest`.
HOSTEL_FEE = 900

# The only genders a block may be created for. Allocation groups participants
# by this exact axis (`allocate_hostels` below), so anything outside this pair
# would be a block nobody's profile.gender could ever match.
GENDERS = {"male", "female"}

# The two roles a hostel_team member may hold. Both can scan (subject to their
# own `attendance` flag) — `hostel_volunteer` additionally handles on-ground
# issues raised against the block, `guard` is scanning-only. The second role's
# exact name is still provisional and may be renamed later; the set itself is
# what is enforced.
HOSTEL_ROLES = {"hostel_volunteer", "guard"}


class HostelCreateRequest(BaseModel):
    name: str
    capacity: int = Field(..., gt=0)
    gender: str  # male | female — drives random allocation grouping
    sharing: int = Field(..., gt=0, description="Max occupants per room")
    num_rooms: int = Field(..., gt=0, description="Number of rooms to pre-generate")

    @field_validator("gender")
    @classmethod
    def _valid_gender(cls, v):
        v = v.strip().lower()
        if v not in GENDERS:
            raise ValueError(f"gender must be one of {sorted(GENDERS)}")
        return v

    @model_validator(mode="after")
    def _rooms_cover_capacity(self):
        if self.num_rooms * self.sharing < self.capacity:
            raise ValueError(
                f"num_rooms ({self.num_rooms}) * sharing ({self.sharing}) must be >= capacity ({self.capacity})"
            )
        return self


class HostelUpdateRequest(BaseModel):
    """
    Every field optional: a caller updates only what it names. Mirrors
    `mess.MessUpdateRequest`'s shape and the same reasoning: `PATCH`-like
    semantics on top of a `PUT`, because the two admin dashboards this backs
    (mess halls, hostel blocks) always edit one field of a body at a time.

    `gender` and the rooming fields (`sharing`, `num_rooms`) are deliberately
    absent — the same restriction `HostelCreateRequest`'s own doc comment
    already states for `gender` ("cannot be changed afterwards by any route in
    this file"), extended to rooming for the same reason: `rooms` holds live
    `occupants` arrays keyed by room index, and neither growing nor shrinking
    that array can be done as a scalar update to `name`/`capacity`
    can — it needs a real migration of who is assigned where, which is exactly
    the "an organiser's decision, not a seed script's" judgement `seed.py`
    already makes about hostel drift in general. Only the two fields with no
    such structural entanglement are editable here.
    """
    name: Optional[str] = None
    capacity: Optional[int] = Field(None, gt=0)


class HostelAssignTeamRequest(BaseModel):
    user_id: str  # must reference an existing backend_teams member with role "other"
    role: str  # hostel_volunteer | guard
    attendance: bool = True  # whether this member may scan entries/exits

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v):
        v = v.strip().lower()
        if v not in HOSTEL_ROLES:
            raise ValueError(f"role must be one of {sorted(HOSTEL_ROLES)}")
        return v


@router.post("")
def create_hostel(request: HostelCreateRequest, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user, "create")

    hostel_id = generator.next_id()

    # The id generator is an in-memory counter that restarts from its seed on every
    # process restart and never consults the database, so a restarted process
    # re-issues ids it has already handed out. The unique constraint on `hostel_id`
    # prevents two blocks sharing one readable identifier; check here to record a
    # useful diagnostic before the constraint rejects the insert.
    with SessionLocal() as session:
        exists = session.scalar(select(Hostel.id).where(Hostel.hostel_id == hostel_id)) is not None
    if exists:
        log_integrity(
            "generated hostel_id already exists — the in-memory counter has wrapped after a restart",
            reason="hostel_id_collision",
            details={"hostel_id": hostel_id, "generator_prefix": "HSTL"},
            actor=current_user,
            action="ID_COLLISION",
            target_id=hostel_id,
            audit=True,
        )
    now = datetime.now(timezone.utc)
    try:
        with SessionLocal.begin() as session:
            hostel = Hostel(
                hostel_id=hostel_id,
                name=request.name,
                capacity=request.capacity,
                gender=request.gender,
                sharing=request.sharing,
                current_occupancy=0,
                created_at=now,
                updated_at=now,
            )
            session.add(hostel)
            session.flush()
            for room_number in generate_room_numbers(request.num_rooms):
                session.add(HostelRoom(
                    hostel_id=hostel.id,
                    room_number=room_number,
                    capacity=request.sharing,
                ))
    except IntegrityError as exc:
        log_integrity(
            "hostel id already exists",
            reason="hostel_id_collision",
            details={"hostel_id": hostel_id, "generator_prefix": "HSTL"},
            actor=current_user,
            action="ID_COLLISION",
            target_id=hostel_id,
            audit=True,
        )
        raise HTTPException(status_code=409, detail="A hostel with this id already exists") from exc
    log_audit(current_user, "CREATE_HOSTEL", hostel_id, {
        "capacity": request.capacity, "sharing": request.sharing, "num_rooms": request.num_rooms,
        # `gender` decides which participants allocation will ever consider for this
        # block, and it cannot be changed afterwards by any route in this file, so
        # this row is the only record of that decision.
        "gender": request.gender,
        "name": request.name,
        "beds": request.num_rooms * request.sharing,
    })
    return {"message": "Hostel created", "hostel_id": hostel_id}

@router.put("/{hostel_id}")
def update_hostel(
    hostel_id: str, request: HostelUpdateRequest, current_user: dict = Depends(get_current_staff)
):
    """
    Edit a block's `name` or `capacity`, Super Admin only.

    Mirrors `mess.update_mess`: every field optional, nothing to update is a
    400, and a `capacity` cut below the block's real bed ceiling
    (`min(capacity, sharing * num_rooms)`, the same ceiling `allocate_hostels`
    enforces) is logged rather than refused — the residents already assigned
    keep their beds, and allocation simply stops placing anyone new here until
    capacity is raised again or beds free up. Rooming (`sharing`/`num_rooms`)
    and `gender` are not editable through this route; see
    `HostelUpdateRequest`.
    """
    _require_super_admin(current_user, "update")

    with SessionLocal() as session:
        existing_row = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
        existing = _hostel_record(session, existing_row) if existing_row else None
    if not existing:
        log_denied(
            current_user, "UPDATE_HOSTEL_DENIED", hostel_id,
            reason="hostel_not_found", details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Hostel not found")

    update_data = {k: v for k, v in request.model_dump(exclude_unset=True).items() if v is not None}
    if not update_data:
        log_denied(
            current_user, "UPDATE_HOSTEL_DENIED", hostel_id,
            reason="nothing_to_update", details={"status": 400}, audit=False,
        )
        raise HTTPException(status_code=400, detail="Nothing to update")

    beds = existing.get("sharing", 1) * len(existing.get("rooms") or [])
    real_ceiling = min(existing.get("capacity", 0), beds)
    if "capacity" in update_data and update_data["capacity"] < real_ceiling:
        log_config.warning(
            _log,
            "hostel capacity reduced below the block's real bed ceiling",
            {
                "hostel_id": hostel_id,
                "reason": "capacity_below_occupancy",
                "new_capacity": update_data["capacity"],
                "previous_capacity": existing.get("capacity"),
                "current_occupancy": existing.get("current_occupancy"),
                "beds": beds,
            },
        )

    update_data["updated_at"] = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        hostel = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
        if hostel is not None:
            for key in ("name", "capacity"):
                if key in update_data:
                    setattr(hostel, key, update_data[key])
            hostel.updated_at = update_data["updated_at"]
    log_audit(
        current_user,
        "UPDATE_HOSTEL",
        hostel_id,
        {
            **update_data,
            "previous_name": existing.get("name") if "name" in update_data else None,
            "previous_capacity": existing.get("capacity") if "capacity" in update_data else None,
            "current_occupancy": existing.get("current_occupancy"),
        },
    )
    return {"message": "Hostel updated"}


@router.get("")
def list_hostels(current_user: dict = Depends(get_current_user)):
    with SessionLocal() as session:
        rows = session.scalars(select(Hostel).order_by(Hostel.hostel_id)).all()
        return [_hostel_record(session, row, expose_internal_id=False) for row in rows]

@router.post("/{hostel_id}/team")
def assign_hostel_team(hostel_id: str, request: HostelAssignTeamRequest, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user, "assign_team")

    with SessionLocal() as session:
        hostel_id_ref = session.scalar(select(Hostel.id).where(Hostel.hostel_id == hostel_id))
    if hostel_id_ref is None:
        log_denied(
            current_user, "ASSIGN_HOSTEL_TEAM_DENIED", hostel_id,
            reason="hostel_not_found",
            details={"team_user_id": request.user_id, "status": 404},
        )
        raise HTTPException(status_code=404, detail="Hostel not found")

    # `user_id` must be a real backend_teams member, and specifically one
    # created with role "other" — the bucket hostel_volunteer/guard staff are
    # created under (see backend_teams.py). This stops a super_admin's own
    # paradox_id, or an admin from another department, being added as a block's
    # duty staff by mistake.
    with SessionLocal() as session:
        staff_row = session.scalar(select(StaffUser).where(
            StaffUser.paradox_id == request.user_id, StaffUser.role == "other"
        ))
        actual = session.scalar(select(StaffUser).where(StaffUser.paradox_id == request.user_id))
    staff = staff_row
    if not staff:
        # The message says what is required but not what was found, and the two
        # failures behind it are different: no such staff account at all, versus an
        # account that exists with a role other than `other`. The second is the
        # confusing one — an admin trying to put a `volunteer` on a hostel door.
        log_denied(
            current_user, "ASSIGN_HOSTEL_TEAM_DENIED", hostel_id,
            reason="staff_role_not_other" if actual else "staff_not_found",
            details={
                "team_user_id": request.user_id,
                "requested_role": request.role,
                "actual_staff_role": actual.role if actual else None,
            },
        )
        raise HTTPException(
            status_code=404,
            detail="user_id must reference an existing backend_teams member with role 'other'"
        )

    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        membership = session.scalar(select(HostelStaff).where(
            HostelStaff.hostel_id == hostel_id_ref,
            HostelStaff.staff_id == staff.id,
        ))
        if membership:
            existing = True
        else:
            session.add(HostelStaff(
                hostel_id=hostel_id_ref,
                staff_id=staff.id,
                role=request.role,
                scanning_enabled=request.attendance,
                added_at=now,
            ))
            existing = False
    if existing:
        log_denied(
            current_user, "ASSIGN_HOSTEL_TEAM_DENIED", hostel_id,
            reason="already_on_team",
            details={"team_user_id": request.user_id, "role": request.role},
        )
        raise HTTPException(status_code=409, detail="Team member already assigned to this hostel")

    log_audit(current_user, "ASSIGN_HOSTEL_TEAM", hostel_id, {
        "team_user_id": request.user_id, "role": request.role,
        # Whether this member can actually scan on arrival. A guard assigned with
        # `attendance=False` is on the roster and refused at the door, which looks
        # identical to a broken scanner from where they are standing.
        "scanning_enabled": request.attendance,
    })
    if not request.attendance:
        log_config.warning(
            _log,
            "hostel team member assigned with scanning disabled",
            {
                "hostel_id": hostel_id,
                "team_user_id": request.user_id,
                "role": request.role,
                "reason": "assigned_without_scanning",
            },
        )
    return {"message": "Team member assigned"}

@router.put("/{hostel_id}/team/{team_user_id}/toggle_scan")
def toggle_hostel_scan(hostel_id: str, team_user_id: str, attendance: bool, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user, "toggle_scan")

    # A filter matching nothing is a 404, as in the mess counterpart. It used to
    # answer "Scanning toggled" and record the miss only as `applied: false`, so a
    # warden who believed they had re-enabled a guard had changed nothing.
    with SessionLocal() as session:
        hostel = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
        staff_id = session.scalar(select(StaffUser.id).where(StaffUser.paradox_id == team_user_id))
        membership = session.get(HostelStaff, (hostel.id, staff_id)) if hostel and staff_id else None
    if not hostel:
        log_denied(
            current_user, "TOGGLE_HOSTEL_SCAN_DENIED", hostel_id,
            reason="hostel_not_found",
            details={"team_user_id": team_user_id, "requested_state": attendance,
                     "status": 404},
        )
        raise HTTPException(status_code=404, detail="Hostel not found")

    if membership is None:
        log_denied(
            current_user, "TOGGLE_HOSTEL_SCAN_DENIED", hostel_id,
            reason="team_member_not_found",
            details={"team_user_id": team_user_id, "requested_state": attendance,
                     "status": 404},
        )
        raise HTTPException(status_code=404, detail="user_id is not on this hostel's team")

    with SessionLocal.begin() as session:
        membership = session.get(HostelStaff, (hostel.id, staff_id))
        if membership:
            membership.scanning_enabled = attendance

    # Previously unaudited, like its mess counterpart. Revoking a guard's scanning
    # at a hostel door is the difference between residents getting in and not, so
    # it belongs in the durable trail.
    log_audit(current_user, "TOGGLE_HOSTEL_SCAN", hostel_id, {
        "team_user_id": team_user_id,
        "scanning_enabled": attendance,
    })
    return {"message": "Scanning toggled"}

@router.post("/allocate")
def allocate_hostels(current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user, "allocate")
    skipped: list[dict] = []
    with SessionLocal.begin() as session:
        # Lock the candidate set and inventory for this allocation pass. This
        # prevents concurrent admin runs from assigning the same participant or
        # overfilling a room.
        hostels = session.scalars(
            select(Hostel).where(Hostel.gender.in_(GENDERS)).order_by(Hostel.hostel_id).with_for_update()
        ).all()
        candidates = session.scalars(
            select(Participant)
            .outerjoin(AccommodationAllocation, AccommodationAllocation.participant_id == Participant.id)
            .where(
                Participant.accommodation_state["registered"].as_boolean().is_(True),
                AccommodationAllocation.participant_id.is_(None),
            )
            .order_by(Participant.created_at, Participant.participant_id)
            .with_for_update(of=Participant)
        ).all()
        grouped = {gender: [h for h in hostels if h.gender == gender] for gender in GENDERS}
        room_by_hostel: dict[uuid.UUID, list[HostelRoom]] = {}
        room_counts: dict[uuid.UUID, int] = {}
        for hostel in hostels:
            rooms = session.scalars(
                select(HostelRoom).where(HostelRoom.hostel_id == hostel.id).order_by(HostelRoom.room_number).with_for_update()
            ).all()
            room_by_hostel[hostel.id] = rooms
            for room in rooms:
                room_counts[room.id] = session.scalar(
                    select(func.count()).select_from(AccommodationAllocation)
                    .where(AccommodationAllocation.room_id == room.id)
                ) or 0

        log_config.info(_log, f"hostel allocation starting for {len(candidates)} candidate(s)", {
            "candidates": len(candidates),
            "blocks": len(hostels),
            "genders_available": sorted(grouped),
            "beds_free": sum(max(sum(r.capacity for r in room_by_hostel[h.id]) - h.current_occupancy, 0) for h in hostels),
        })

        allocated = 0
        skipped_by_reason: Dict[str, int] = {}
        for participant in candidates:
            gender = (participant.gender or "").lower()
            eligible = grouped.get(gender, [])
            placed = False
            for hostel in eligible:
                beds = sum(room.capacity for room in room_by_hostel[hostel.id])
                if hostel.current_occupancy >= min(hostel.capacity, beds):
                    continue
                room = next((r for r in room_by_hostel[hostel.id] if room_counts[r.id] < r.capacity), None)
                if room is None:
                    continue
                session.add(AccommodationAllocation(
                    participant_id=participant.id,
                    room_id=room.id,
                    status="allocated",
                    is_inside=False,
                ))
                hostel.current_occupancy += 1
                room_counts[room.id] += 1
                allocated += 1
                placed = True
                log_config.debug(_log, "hostel bed assigned", {
                    "participant_id": participant.participant_id,
                    "hostel_id": hostel.hostel_id,
                    "room": room.room_number,
                    "gender": gender,
                })
                break
            if not placed:
                reason = "missing_gender" if not gender else "no_block_for_gender" if not eligible else "capacity_exhausted"
                skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
                skipped.append({
                    "participant_id": participant.participant_id,
                    "reason": reason,
                    "gender": gender or None,
                    "blocks_for_gender": [h.hostel_id for h in eligible],
                    "genders_available": sorted(grouped),
                })
        beds_remaining = {
            h.hostel_id: max(min(h.capacity, sum(r.capacity for r in room_by_hostel[h.id])) - h.current_occupancy, 0)
            for h in hostels
        }
        candidate_count = len(candidates)
        unplaceable = sum(skipped_by_reason.values())

    for item in skipped:
        log_denied(current_user, "HOSTEL_ALLOCATION_SKIPPED", item["participant_id"],
                   reason=item["reason"], details={k: v for k, v in item.items() if k not in {"participant_id", "reason"}})
    log_batch(
        current_user,
        "ALLOCATE_HOSTELS",
        None,
        {
            "allocated_count": allocated,
            "candidates": candidate_count,
            "skipped_count": unplaceable,
            "skipped_by_reason": skipped_by_reason,
            "beds_remaining": beds_remaining,
        },
    )
    if unplaceable:
        log_config.warning(
            _log,
            f"hostel allocation left {unplaceable} of {candidate_count} candidate(s) unplaced",
            {
                "allocated": allocated,
                "skipped": unplaceable,
                "skipped_by_reason": skipped_by_reason,
                "reason": "incomplete_allocation",
            },
        )
    return {"message": f"Allocated {allocated} participants to hostels"}

@router.post("/pay")
def pay_hostel_fee(request: MockPaymentRequest, current_user: dict = Depends(get_current_participant)):
    """
    Simulate settling the hostel fee.

    Mock end to end: there is no real gateway behind this, `simulate_payment`
    always succeeds today, and `HOSTEL_FEE` is the only amount this can ever
    charge — never one the client supplies. Deliberately independent of
    `accommodation.registered` / `accommodation.hostel_id`: this only records
    that the fee was paid, it does not opt a participant into allocation or
    place them in a block, so it can be called in any order relative to those.
    """
    if "participant_id" not in current_user:
        log_denied(
            current_user, "HOSTEL_PAYMENT_DENIED", None,
            reason="not_a_participant", details={"status": 400}, audit=False,
        )
        raise HTTPException(status_code=400, detail="Only participants can pay the hostel fee")

    existing = (current_user.get("accommodation") or {}).get("payment")
    payment = simulate_payment("hostel", HOSTEL_FEE, request.method, purpose_actor=current_user)

    if existing:
        # Same non-idempotency as the mess fee: the prior transaction id is about to
        # be overwritten and this line is the only place it survives.
        log_config.warning(
            _log,
            "hostel payment overwrote an existing payment record",
            {
                "participant_id": current_user.get("participant_id"),
                "reason": "payment_overwritten",
                "previous_transaction_id": existing.get("transaction_id"),
                "previous_amount": existing.get("amount"),
                "previous_paid_at": existing.get("paid_at"),
                "new_transaction_id": payment["transaction_id"],
            },
        )

    with SessionLocal.begin() as session:
        session.add(Payment(
            participant_id=uuid.UUID(str(current_user["_id"])),
            facility_type="hostel",
            facility_id="accommodation",
            transaction_id=payment["transaction_id"],
            amount=payment["amount"],
            status="paid",
            method=payment.get("method"),
            settled_at=payment["paid_at"],
            details={"simulated": True},
        ))
    log_audit(current_user, "HOSTEL_PAYMENT", current_user.get("participant_id"), {
        "transaction_id": payment["transaction_id"], "amount": payment["amount"],
        "method": payment.get("method"),
        "replaced_transaction_id": (existing or {}).get("transaction_id"),
        "registered_for_accommodation": bool((current_user.get("accommodation") or {}).get("registered")),
    })
    return payment


@router.post("/register")
def register_for_accommodation(current_user: dict = Depends(get_current_participant)):
    """
    Ask for a hostel place during the fest.

    `POST /hostels/allocate` only considers participants whose
    `accommodation.registered` is True, and registration sets that flag False.
    This is what lets a participant opt in, so allocation has anyone to place.
    Declared before `/{hostel_id}/...` routes so the literal path is not
    captured as a hostel id.

    Idempotent: asking twice is not an error, it just stays requested.
    """
    if "participant_id" not in current_user:
        raise HTTPException(status_code=400, detail="Only participants can request accommodation")

    if current_user.get("accommodation", {}).get("hostel_id"):
        log_denied(
            current_user, "ACCOMMODATION_REGISTER_DENIED",
            current_user.get("participant_id"),
            reason="already_allotted",
            details={"hostel_id": current_user.get("accommodation", {}).get("hostel_id")},
        )
        raise HTTPException(status_code=400, detail="Accommodation already allotted")

    with SessionLocal.begin() as session:
        participant = session.get(Participant, uuid.UUID(str(current_user["_id"])))
        if participant is not None:
            participant.accommodation_state = {**(participant.accommodation_state or {}), "registered": True}
    # `target_id` was None. The participant id goes in it, so a person's
    # accommodation history — requested, cancelled, allotted, scanned in — reads as
    # one filterable sequence. Whether their profile carries a usable gender is
    # recorded here because that is what decides, minutes or days later, whether
    # allocation can place them at all.
    log_audit(current_user, "ACCOMMODATION_REGISTER", current_user.get("participant_id"), {
        "gender": ((current_user.get("profile") or {}).get("gender") or "").lower() or None,
        "already_paid": bool((current_user.get("accommodation") or {}).get("payment")),
    })
    return {"message": "Accommodation requested"}


@router.delete("/register")
def cancel_accommodation_request(current_user: dict = Depends(get_current_participant)):
    """
    Withdraw a pending accommodation request.

    Refused once a hostel has been allotted: releasing an allocated bed is an
    organiser decision, not a self-service one.
    """
    if "participant_id" not in current_user:
        raise HTTPException(status_code=400, detail="Only participants can cancel accommodation")

    if current_user.get("accommodation", {}).get("hostel_id"):
        log_denied(
            current_user, "ACCOMMODATION_CANCEL_DENIED",
            current_user.get("participant_id"),
            reason="already_allotted",
            details={"hostel_id": current_user.get("accommodation", {}).get("hostel_id")},
        )
        raise HTTPException(status_code=400, detail="Accommodation already allotted")

    with SessionLocal.begin() as session:
        participant = session.get(Participant, uuid.UUID(str(current_user["_id"])))
        if participant is not None:
            participant.accommodation_state = {**(participant.accommodation_state or {}), "registered": False}
    log_audit(current_user, "ACCOMMODATION_CANCEL", current_user.get("participant_id"), {
        "was_registered": bool((current_user.get("accommodation") or {}).get("registered")),
    })
    return {"message": "Accommodation request withdrawn"}


@router.get("/my_hostel")
def my_hostel(current_user: dict = Depends(get_current_participant)):
    if "participant_id" not in current_user:
        raise HTTPException(status_code=400, detail="Only participants have assigned hostels")

    accommodation = current_user.get("accommodation", {}) or {}
    hostel_id = accommodation.get("hostel_id")
    hostel_details = None
    volunteers = []
    if hostel_id:
        with SessionLocal() as session:
            hostel = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
            if hostel:
                hostel_details = _hostel_record(session, hostel, expose_internal_id=False)
                team_rows = session.execute(
                    select(HostelStaff, StaffUser, Participant)
                    .join(StaffUser, StaffUser.id == HostelStaff.staff_id)
                    .outerjoin(Participant, Participant.id == StaffUser.participant_id)
                    .where(HostelStaff.hostel_id == hostel.id)
                    .order_by(StaffUser.paradox_id)
                ).all()
                for membership, staff, participant in team_rows:
                    attrs = staff.attributes or {}
                    volunteers.append({
                        "name": attrs.get("name") or attrs.get("designation"),
                        "email": staff.email,
                        "phone": participant.phone if participant else None,
                        "role": membership.role,
                    })

    return {
        "assigned_hostel": hostel_id,
        "room": accommodation.get("room"),
        "inside": accommodation.get("inside", False),
        "arrival": accommodation.get("arrival"),
        "departure": accommodation.get("departure"),
        # Whether this participant has asked for accommodation at all. Without
        # it the UI cannot tell "never requested" from "requested, not yet
        # allocated" — two states that need very different things said to them.
        "registered": accommodation.get("registered", False),
        "volunteers": volunteers
    }

@router.post("/{hostel_id}/scan")
def scan_hostel(hostel_id: str, request: ScanQRRequest, action: str, current_user: dict = Depends(get_current_staff)):
    """
    Record one participant crossing a hostel door.

    The most safety-relevant endpoint in the system: `accommodation.inside` is what
    answers "who is in this building" if the building ever has to be evacuated, and
    `arrival` / `departure` are what answer "was this student ever here at all".

    Every refusal is now recorded with the state that caused it. Before this, a
    refused scan produced a 400 at the door and nothing else — so a resident
    insisting they had been let out, against a record saying they were still
    inside, was an argument with no evidence on either side. The state triple
    (`inside`, `arrival`, `departure`) goes into every line precisely so that
    argument becomes answerable.
    """
    # action: "entry" | "exit" | "permanent_exit"
    if action not in ("entry", "exit", "permanent_exit"):
        log_denied(
            current_user, "HOSTEL_SCAN_DENIED", hostel_id,
            reason="invalid_action", details={"action": action}, audit=False,
        )
        raise HTTPException(status_code=400, detail="Invalid action. Must be 'entry', 'exit', or 'permanent_exit'")

    user_id = current_user.get("paradox_id")
    with SessionLocal() as session:
        hostel_row = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
        staff_id = session.scalar(select(StaffUser.id).where(StaffUser.paradox_id == user_id))
        membership = session.get(HostelStaff, (hostel_row.id, staff_id)) if hostel_row and staff_id else None
        hostel = _hostel_record(session, hostel_row) if hostel_row else None
    if not hostel:
        log_denied(
            current_user, "HOSTEL_SCAN_DENIED", hostel_id,
            reason="hostel_not_found", details={"action": action},
        )
        raise HTTPException(status_code=404, detail="Hostel not found")

    team_member = ({
        "user_id": user_id,
        "role": membership.role,
        "attendance": membership.scanning_enabled,
    } if membership else None)

    if not team_member:
        log_denied(
            current_user, "HOSTEL_SCAN_DENIED", hostel_id,
            reason="not_on_hostel_team",
            details={"action": action, "team_size": len(hostel.get("hostel_team") or [])},
        )
        raise HTTPException(status_code=403, detail="Not authorized to scan for this hostel")

    if not team_member.get("attendance"):
        # The door-side symptom of `TOGGLE_HOSTEL_SCAN`, now joinable to it.
        log_denied(
            current_user, "HOSTEL_SCAN_DENIED", hostel_id,
            reason="scanning_disabled_for_member",
            details={"action": action, "member_role": team_member.get("role")},
        )
        raise HTTPException(status_code=403, detail="Scanning disabled for you")

    target_user, _ = verify_qr(request, actor=current_user, domain="hostel", target_id=hostel_id)

    user_acc = target_user.get("accommodation", {}) or {}
    with SessionLocal() as session:
        target_participant_id = uuid.UUID(str(target_user["_id"]))
        allocation = session.scalar(
            select(AccommodationAllocation)
            .join(HostelRoom, AccommodationAllocation.room_id == HostelRoom.id)
            .join(Hostel, HostelRoom.hostel_id == Hostel.id)
            .where(
                AccommodationAllocation.participant_id == target_participant_id,
                Hostel.hostel_id == hostel_id,
            )
        )
        if allocation:
            allocation_snapshot = {
                "is_inside": allocation.is_inside,
                "arrival_at": allocation.arrival_at,
                "departure_at": allocation.departure_at,
            }
        else:
            allocation_snapshot = None
    if allocation_snapshot is None:
        log_scan(
            current_user, "hostel", "HOSTEL_SCAN_DENIED", OUTCOME_DENIED,
            participant_id=target_user.get("participant_id"),
            target_id=hostel_id,
            reason="not_allotted_to_this_hostel",
            details={
                "action": action,
                "allotted_hostel_id": user_acc.get("hostel_id"),
                "registered_for_accommodation": bool(user_acc.get("registered")),
            },
        )
        raise HTTPException(status_code=400, detail="Participant not allotted to this hostel")

    is_inside = allocation_snapshot["is_inside"]
    has_departed = allocation_snapshot["departure_at"] is not None
    now = datetime.now(timezone.utc)
    update_fields = {}

    # The state every line below reports, gathered once. This is what makes a
    # refusal reconstructable: not just "already inside", but since when, and
    # whether they had ever arrived or departed.
    state = {
        "action": action,
        "inside": is_inside,
        "arrival": allocation_snapshot["arrival_at"].isoformat() if allocation_snapshot["arrival_at"] else None,
        "departure": allocation_snapshot["departure_at"].isoformat() if allocation_snapshot["departure_at"] else None,
        "room": user_acc.get("room"),
    }

    def refuse(reason: str, detail: str):
        log_scan(
            current_user, "hostel", "HOSTEL_SCAN_DENIED", OUTCOME_DENIED,
            participant_id=target_user.get("participant_id"),
            target_id=hostel_id,
            reason=reason,
            details=state,
        )
        raise HTTPException(status_code=400, detail=detail)

    if action == "entry":
        if has_departed:
            # A participant who has formally left the fest being scanned back in.
            # Either they returned and somebody needs to reverse the departure, or
            # the permanent exit was scanned by mistake earlier. Both need the
            # departure timestamp, which is in `state`.
            refuse("already_permanently_departed", "Participant has permanently departed and cannot re-enter")
        if is_inside:
            # Usually a double scan at the door; occasionally a missed exit, which
            # means the building's occupancy figure is already wrong.
            refuse("already_inside", "Participant is already inside")
        update_fields["accommodation.inside"] = True
        # Stamped only the first time, ever — a returning-next-year re-entry
        # scan (were it ever allowed) should not overwrite the original arrival.
        if not allocation_snapshot["arrival_at"]:
            update_fields["accommodation.arrival"] = now
    elif action == "exit":
        if not is_inside:
            refuse("already_outside", "Participant is already outside")
        update_fields["accommodation.inside"] = False
    else:  # permanent_exit
        if has_departed:
            refuse("already_permanently_departed", "Participant has already permanently departed")
        if not is_inside:
            refuse("not_inside_for_permanent_exit", "Participant must be inside the hostel to mark a permanent exit")
        update_fields["accommodation.inside"] = False
        update_fields["accommodation.departure"] = now

    with SessionLocal.begin() as session:
        allocation = session.scalar(
            select(AccommodationAllocation)
            .join(HostelRoom, AccommodationAllocation.room_id == HostelRoom.id)
            .join(Hostel, HostelRoom.hostel_id == Hostel.id)
            .where(
                AccommodationAllocation.participant_id == target_participant_id,
                Hostel.hostel_id == hostel_id,
            ).with_for_update()
        )
        changed = allocation is not None
        if allocation is not None:
            allocation.is_inside = update_fields["accommodation.inside"]
            if "accommodation.arrival" in update_fields:
                allocation.arrival_at = now
            if "accommodation.departure" in update_fields:
                allocation.departure_at = now
    if not changed:
        # The door reported success while the record did not move. For this
        # endpoint that is worse than an error: the occupancy list is now wrong in
        # a way nobody has been told about.
        log_integrity(
            "hostel scan did not change the participant's state",
            reason="scan_write_not_applied",
            details={
                "participant_id": target_user.get("participant_id"),
                "hostel_id": hostel_id,
                "action": action,
                "matched": 0,
                "fields": sorted(update_fields.keys()),
            },
        )

    log_scan(
        current_user, "hostel", f"HOSTEL_{action.upper()}", OUTCOME_ALLOWED,
        participant_id=target_user.get("participant_id"),
        target_id=hostel_id,
        # `participant_id` keeps its original place in `details` via `log_scan`, so
        # existing per-entity views and exports are unaffected. The transition is
        # additive: `inside_before` / `inside_after` is what lets a day's door
        # traffic be replayed as a sequence rather than a set of point-in-time rows.
        details={
            "inside_before": is_inside,
            "inside_after": update_fields.get("accommodation.inside"),
            "arrival_stamped": "accommodation.arrival" in update_fields,
            "departure_stamped": "accommodation.departure" in update_fields,
            "room": user_acc.get("room"),
        },
    )
    return {"message": f"Scan successful, {action} allowed"}

@router.delete("/{hostel_id}")
def delete_hostel(hostel_id: str, current_user: dict = Depends(get_current_staff)):
    """
    Delete a hostel block, Super Admin only.

    Cascades: every participant currently pointing at this hostel has their
    accommodation reset (hostel_id, room, inside, arrival, departure) so they
    are no longer marked as living somewhere that no longer exists.
    `registered` is left untouched, since they still want a bed — just not this
    one — and should be eligible for a future `/allocate` run.
    """
    _require_super_admin(current_user, "delete")

    with SessionLocal() as session:
        hostel_row = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
        hostel = _hostel_record(session, hostel_row) if hostel_row else None
    if hostel_row is None:
        log_denied(
            current_user, "DELETE_HOSTEL_DENIED", hostel_id,
            reason="hostel_not_found", details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Hostel not found")

    # Captured before the cascade, because afterwards nothing links these people to
    # this block. This is the most destructive operation in the file: it discards
    # `arrival` and `departure` for everybody who lived here, which is the record of
    # whether they were ever physically present at the fest. Those facts survive
    # only in the `HOSTEL_ENTRY` / `HOSTEL_EXIT` audit rows and in this one.
    with SessionLocal.begin() as session:
        hostel_row = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id).with_for_update())
        if hostel_row is None:
            raise HTTPException(status_code=404, detail="Hostel not found")
        room_rows = session.scalars(select(HostelRoom).where(HostelRoom.hostel_id == hostel_row.id)).all()
        allocations = session.execute(
            select(AccommodationAllocation, Participant, HostelRoom)
            .join(Participant, Participant.id == AccommodationAllocation.participant_id)
            .join(HostelRoom, HostelRoom.id == AccommodationAllocation.room_id)
            .where(HostelRoom.hostel_id == hostel_row.id)
        ).all()
        affected = [{
            "participant_id": participant.participant_id,
            "room": room.room_number,
            "was_inside": allocation.is_inside,
            "had_arrived": bool(allocation.arrival_at),
        } for allocation, participant, room in allocations]
        still_inside = [row["participant_id"] for row in affected if row["was_inside"]]
        reset_count = len(allocations)
        for allocation, _, _ in allocations:
            session.delete(allocation)
        for room in room_rows:
            session.delete(room)
        session.delete(hostel_row)
    still_inside = [row["participant_id"] for row in affected if row["was_inside"]]
    log_audit(current_user, "DELETE_HOSTEL", hostel_id, {
        "participants_reset": reset_count,
        "name": hostel.get("name"),
        "gender": hostel.get("gender"),
        "capacity": hostel.get("capacity"),
        "lifetime_occupancy": hostel.get("current_occupancy"),
        "team_size": len(hostel.get("hostel_team") or []),
        "residents": affected,
        "were_inside_at_deletion": still_inside,
    })
    if still_inside:
        # Deleting a block while people are recorded as being inside it means the
        # occupancy record for those people is being erased while they are, as far
        # as the system knew, physically in the building.
        log_config.warning(
            _log,
            f"hostel {hostel_id} deleted while {len(still_inside)} resident(s) were recorded as inside",
            {
                "hostel_id": hostel_id,
                "reason": "deleted_with_residents_inside",
                "participants_inside": still_inside,
                "destructive": True,
            },
        )
    return {"message": "Hostel deleted", "participants_reset": reset_count}

@router.get("/{hostel_id}/statistics")
def hostel_statistics(hostel_id: str, current_user: dict = Depends(get_current_staff)):
    _require_super_admin(current_user, "statistics")

    with SessionLocal() as session:
        hostel = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
        allocations = session.execute(
            select(Participant, AccommodationAllocation, HostelRoom)
            .join(AccommodationAllocation, AccommodationAllocation.participant_id == Participant.id)
            .join(HostelRoom, HostelRoom.id == AccommodationAllocation.room_id)
            .where(HostelRoom.hostel_id == hostel.id if hostel else False)
            .order_by(Participant.participant_id)
        ).all() if hostel else []
    if not hostel:
        log_denied(
            current_user, "READ_HOSTEL_ROSTER_DENIED", hostel_id,
            reason="hostel_not_found", details={"status": 404},
        )
        raise HTTPException(status_code=404, detail="Hostel not found")

    inside_count = sum(1 for _, allocation, _ in allocations if allocation.is_inside)
    allotted = [{
        "participant_id": participant.participant_id,
        "name": participant.full_name,
        "email": participant.email,
        "room": room.room_number,
    } for participant, _, room in allocations]

    # A roster read, recorded like the mess equivalent. `currently_inside` also
    # goes in, because this endpoint is the occupancy snapshot somebody would be
    # asked to produce after an incident, and knowing what it said at the time it
    # was read is part of that.
    log_audit(current_user, "READ_HOSTEL_ROSTER", hostel_id, {
        "returned": len(allocations),
        "currently_inside": inside_count,
        "capacity": hostel.capacity,
    })

    return {
        "total_allocated": len(allocations),
        "capacity": hostel.capacity,
        "current_occupancy": hostel.current_occupancy,
        "currently_inside": inside_count,
        "allotted_participants": allotted
    }

@router.get("/{hostel_id}")
def get_hostel(hostel_id: str, current_user: dict = Depends(get_current_user)):
    """A single hostel's document. Declared last so it never captures a literal
    path (`/allocate`, `/register`, `/my_hostel`) defined earlier in this file."""
    with SessionLocal() as session:
        hostel = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
        if not hostel:
            raise HTTPException(status_code=404, detail="Hostel not found")
        return _hostel_record(session, hostel, expose_internal_id=False)
