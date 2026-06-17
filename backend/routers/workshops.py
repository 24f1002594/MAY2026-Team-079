"""Workshop catalogue, bookings and attendance backed by PostgreSQL."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

import log_config
from db import SessionLocal
from dependencies import get_current_participant, get_current_staff, get_current_user, verify_qr
from embedding_service import generate_embedding
from id_generator import SequentialIDGenerator
from logger import OUTCOME_ALLOWED, OUTCOME_DENIED, OUTCOME_DUPLICATE, log_audit, log_denied, log_integrity, log_scan
from models import ScanQRRequest, WorkshopAssignVolunteerRequest, WorkshopCreateRequest, WorkshopParticipantUpdateRequest, WorkshopUpdateRequest, parse_instant_utc
from schema import Participant, StaffUser, Workshop, WorkshopAttendance, WorkshopLog, WorkshopRegistration, WorkshopSlot, WorkshopStaff

router = APIRouter(prefix="/workshops", tags=["Workshops"])
generator = SequentialIDGenerator("WKSP")
_log = log_config.get_logger("paradox.workshops")
_WINDOW_OPEN_MINUTES = {"pre-registered": 30, "on-spot": 15, "changes": 0}
_WINDOW_CLOSE_MINUTES = 30


def _row(session, workshop: Workshop, *, include_team=True):
    config, attrs = workshop.registration_config or {}, workshop.attributes or {}
    slot = session.get(WorkshopSlot, workshop.slot_id)
    registered = session.scalar(select(func.count()).select_from(WorkshopRegistration).where(
        WorkshopRegistration.workshop_id == workshop.id, WorkshopRegistration.status == "registered")) or 0
    attended = session.scalar(select(func.count()).select_from(WorkshopAttendance).where(
        WorkshopAttendance.workshop_id == workshop.id)) or 0
    result = {
        "_id": str(workshop.id), "workshop_id": workshop.workshop_id,
        "slot_id": slot.slot_id if slot else None, "name": workshop.name,
        "description": attrs.get("description", ""), "embedding": attrs.get("embedding") or [],
        "venue": attrs.get("venue"), "capacity": workshop.capacity,
        "registration_count": registered, "participant_count": attended,
        "instructions": attrs.get("instructions"), "start_time": workshop.start_time.isoformat(),
        "registration_start": config.get("registration_start"),
        "registration_end": config.get("registration_end"),
        "registration_open": config.get("registration_open", True),
        "registration_closed_by_system": config.get("registration_closed_by_system", False),
        "workshop_team": [
            {"user_id": s.paradox_id, "role": m.role, "attendance": m.scanning_enabled}
            for m, s in session.execute(select(WorkshopStaff, StaffUser).join(
                StaffUser, StaffUser.id == WorkshopStaff.staff_id).where(
                WorkshopStaff.workshop_id == workshop.id).order_by(StaffUser.paradox_id)).all()
        ],
        "created_by": attrs.get("created_by"), "created_at": workshop.created_at,
        "updated_at": workshop.updated_at,
    }
    if not include_team:
        result.pop("workshop_team")
        result.pop("created_by")
    return result


def _resolve(session, identifier: str, *, lock: bool = False):
    statement = select(Workshop).join(WorkshopSlot, WorkshopSlot.id == Workshop.slot_id).where(
        (Workshop.workshop_id == identifier) | (WorkshopSlot.slot_id == identifier))
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _sync_window(session, row):
    config = dict(row.registration_config or {})
    end = config.get("registration_end")
    if config.get("registration_open") and end and not config.get("registration_closed_by_system"):
        try:
            end_dt = parse_instant_utc(end, "registration_end").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > end_dt:
                config.update(registration_open=False, registration_closed_by_system=True)
                row.registration_config = config
                row.updated_at = datetime.now(timezone.utc)
                session.commit()
        except ValueError:
            log_integrity("workshop registration end is invalid", reason="registration_end_unparseable",
                          details={"workshop_id": row.workshop_id})
    return _row(session, row)


def _staff(session, user_id):
    return session.scalar(select(StaffUser).where(StaffUser.paradox_id == str(user_id)))


def _admin(session, user_id):
    staff = _staff(session, user_id)
    return bool(staff and staff.role == "super_admin")


def _membership(session, workshop, user_id):
    staff = _staff(session, user_id)
    if not staff:
        return None
    return session.scalar(select(WorkshopStaff).where(
        WorkshopStaff.workshop_id == workshop.id, WorkshopStaff.staff_id == staff.id))


def _require_admin(session, user, detail):
    uid = user.get("paradox_id")
    if not _admin(session, uid):
        log_denied(user, "AUTHZ_DENIED", None, reason="not_super_admin",
                   details={"resource": "workshops", "status": 403})
        raise HTTPException(403, detail)
    return uid


def _assert_window(row, operation, actor=None):
    start = row.start_time
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    opens = start - timedelta(minutes=_WINDOW_OPEN_MINUTES[operation])
    closes = start + timedelta(minutes=_WINDOW_CLOSE_MINUTES)
    if now < opens:
        minutes = int((opens - now).total_seconds() // 60)
        log_denied(actor, "WORKSHOP_ATTENDANCE_DENIED", row.workshop_id,
                   reason="window_not_open", details={"operation": operation, "opens_in_minutes": minutes})
        raise HTTPException(403, f"Scanning window not yet open. Opens {_WINDOW_OPEN_MINUTES[operation]} min before start (in ~{minutes} min).")
    if now >= closes:
        log_denied(actor, "WORKSHOP_ATTENDANCE_DENIED", row.workshop_id,
                   reason="window_closed", details={"operation": operation})
        raise HTTPException(403, "Scanning window closed. It closes 30 min after the workshop starts.")


def _log(session, workshop, action, participant=None, actor=None, **details):
    session.add(WorkshopLog(workshop_id=workshop.id if workshop else None,
        participant_id=participant.id if participant else None,
        actor_id=str((actor or {}).get("paradox_id") or "") or None,
        action=action, details=details))


@router.post("")
def create_workshop(request: WorkshopCreateRequest, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        _require_admin(s, current_user, "Only Super Admins can create workshops")
        slot = s.scalar(select(WorkshopSlot).where(WorkshopSlot.slot_id == request.slot_id))
        if not slot:
            raise HTTPException(404, "Workshop slot not found. Create it via POST /workshop-slots first.")
        identifier = generator.next_id()
        config = {"registration_start": request.registration_start, "registration_end": request.registration_end,
                  "registration_open": request.registration_open, "registration_closed_by_system": False}
        row = Workshop(workshop_id=identifier, name=request.name, slot_id=slot.id,
            start_time=slot.start_time, capacity=request.capacity, registration_config=config,
            attributes={"description": request.description, "embedding": generate_embedding(request.description),
                        "venue": request.venue, "instructions": request.instructions,
                        "created_by": current_user.get("_id")})
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise HTTPException(409, "Workshop ID already exists")
        log_audit(current_user, "CREATE_WORKSHOP", identifier, {"capacity": request.capacity, "slot_id": request.slot_id})
        return {"message": "Workshop created", "workshop_id": identifier}


@router.get("")
def list_workshops(current_user=Depends(get_current_user)):
    with SessionLocal() as s:
        admin = _admin(s, current_user.get("paradox_id"))
        rows = s.scalars(select(Workshop).order_by(Workshop.created_at)).all()
        result = []
        for row in rows:
            data = _sync_window(s, row)
            data.pop("registration_closed_by_system", None)
            if not admin:
                data.pop("workshop_team", None)
            data.pop("created_by", None)
            data.pop("_id", None)
            result.append(data)
        return result


@router.get("/public")
def list_public_workshops():
    fields = ("workshop_id", "slot_id", "name", "description", "embedding", "venue", "capacity",
              "registration_count", "instructions", "start_time", "registration_start", "registration_end", "registration_open")
    with SessionLocal() as s:
        result = []
        for row in s.scalars(select(Workshop).order_by(Workshop.created_at)).all():
            data = _sync_window(s, row)
            result.append({key: data.get(key) for key in fields})
        return result


@router.get("/my_registrations")
def my_workshop_registrations(current_user=Depends(get_current_participant)):
    if not current_user.get("participant_id"):
        return []
    with SessionLocal() as s:
        p = s.scalar(select(Participant).where(Participant.participant_id == current_user["participant_id"]))
        if not p:
            return []
        regs = s.scalars(select(WorkshopRegistration).where(
            WorkshopRegistration.participant_id == p.id, WorkshopRegistration.status == "registered")).all()
        result = []
        for reg in regs:
            data = _sync_window(s, reg.workshop)
            attrs = reg.attributes or {}
            result.append({k: data.get(k) for k in ("workshop_id", "slot_id", "name", "description", "embedding", "venue", "start_time", "registration_start", "registration_end", "registration_open")} | {
                "booking_type": attrs.get("booking_type", "pre-registered"),
                "attended": bool(s.scalar(select(func.count()).select_from(WorkshopAttendance).where(
                    WorkshopAttendance.workshop_id == reg.workshop_id,
                    WorkshopAttendance.participant_id == p.id)))})
        return result


@router.put("/{workshop_id}")
def update_workshop(workshop_id: str, request: WorkshopUpdateRequest, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        if not _admin(s, current_user.get("paradox_id")):
            raise HTTPException(403, "Only Super Admins can edit workshops")
        row = s.scalar(select(Workshop).where(Workshop.workshop_id == workshop_id))
        if not row:
            raise HTTPException(404, "Workshop not found")
        values = request.model_dump(exclude_none=True)
        attrs, config = dict(row.attributes or {}), dict(row.registration_config or {})
        if "registration_start" in values or "registration_end" in values:
            merged_start = values.get("registration_start", config.get("registration_start"))
            merged_end = values.get("registration_end", config.get("registration_end"))
            if not merged_start or not merged_end:
                raise HTTPException(422, "registration_start and registration_end are required")
            start_dt = parse_instant_utc(merged_start, "registration_start")
            end_dt = parse_instant_utc(merged_end, "registration_end")
            if end_dt <= start_dt:
                raise HTTPException(400, "registration_end must be after registration_start")
        for key in ("name", "venue", "instructions"):
            if key in values:
                if key == "name": row.name = values[key]
                else: attrs[key] = values[key]
        if "description" in values:
            attrs["description"] = values["description"]
            attrs["embedding"] = generate_embedding(values["description"])
        if "capacity" in values: row.capacity = values["capacity"]
        for key in ("registration_start", "registration_end", "registration_open"):
            if key in values: config[key] = values[key]
        if "registration_end" in values: config["registration_closed_by_system"] = False
        row.attributes, row.registration_config, row.updated_at = attrs, config, datetime.now(timezone.utc)
        s.commit()
        log_audit(current_user, "UPDATE_WORKSHOP", workshop_id, values)
        return {"message": "Workshop updated"}


@router.delete("/{workshop_id}")
def delete_workshop(workshop_id: str, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        _require_admin(s, current_user, "Only Super Admins can delete workshops")
        row = s.scalar(select(Workshop).where(Workshop.workshop_id == workshop_id))
        if not row: raise HTTPException(404, "Workshop not found")
        s.delete(row); s.commit()
        log_audit(current_user, "DELETE_WORKSHOP", workshop_id, {})
        return {"message": "Workshop deleted"}


@router.post("/{workshop_id}/volunteers")
def assign_workshop_volunteer(workshop_id: str, request: WorkshopAssignVolunteerRequest, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        _require_admin(s, current_user, "Only Super Admins can assign volunteers")
        w = _resolve(s, workshop_id); staff = _staff(s, request.user_id)
        if not w: raise HTTPException(404, "Workshop not found")
        if not staff: raise HTTPException(404, "Staff member not found")
        if _membership(s, w, request.user_id): raise HTTPException(409, "Volunteer already assigned to this workshop")
        role = request.role if request.role in ("workshop_head", "member", "volunteer") else "volunteer"
        s.add(WorkshopStaff(workshop_id=w.id, staff_id=staff.id, role=role, scanning_enabled=request.attendance)); s.commit()
        log_audit(current_user, "ASSIGN_WORKSHOP_VOLUNTEER", w.workshop_id, {"volunteer_user_id": request.user_id, "role": role})
        return {"message": "Volunteer assigned"}


@router.put("/{workshop_id}/volunteers/{user_id}/toggle_scan")
def toggle_volunteer_scan(workshop_id: str, user_id: str, attendance: bool, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        _require_admin(s, current_user, "Only Super Admins can toggle scanning")
        w = _resolve(s, workshop_id, lock=True)
        if not w: raise HTTPException(404, "Workshop not found")
        member = _membership(s, w, user_id)
        if not member: raise HTTPException(404, "That member is not on this workshop's team")
        member.scanning_enabled = attendance; s.commit()
        log_audit(current_user, "TOGGLE_WORKSHOP_SCAN", w.workshop_id, {"volunteer_user_id": user_id, "scanning_enabled": attendance})
        return {"message": "Volunteer scanning toggled"}


@router.delete("/{workshop_id}/volunteers/{user_id}")
def remove_workshop_volunteer(workshop_id: str, user_id: str, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        _require_admin(s, current_user, "Only Super Admins can remove volunteers")
        w = _resolve(s, workshop_id)
        if not w: raise HTTPException(404, "Workshop not found")
        member = _membership(s, w, user_id)
        if not member: raise HTTPException(404, "That member is not on this workshop's team")
        s.delete(member); s.commit()
        log_audit(current_user, "REMOVE_WORKSHOP_VOLUNTEER", w.workshop_id, {"user_id": user_id})
        return {"message": "Volunteer removed"}


@router.post("/{workshop_id}/register")
def register_for_workshop(workshop_id: str, current_user=Depends(get_current_participant)):
    with SessionLocal() as s:
        p = s.scalar(select(Participant).where(Participant.participant_id == current_user.get("participant_id")))
        w = _resolve(s, workshop_id, lock=True)
        if not p: raise HTTPException(400, "Only participants can register for workshops")
        if not w: raise HTTPException(404, "Workshop not found")
        data = _sync_window(s, w); config = w.registration_config or {}
        if not data["registration_open"]: raise HTTPException(400, "Registration is closed for this workshop")
        count = s.scalar(select(func.count()).select_from(WorkshopRegistration).where(
            WorkshopRegistration.workshop_id == w.id, WorkshopRegistration.status == "registered")) or 0
        if count >= w.capacity: raise HTTPException(400, "Workshop is full")
        p = s.scalar(select(Participant).where(Participant.id == p.id).with_for_update())
        regs = s.scalars(select(WorkshopRegistration).where(
            WorkshopRegistration.participant_id == p.id, WorkshopRegistration.status == "registered")).all()
        if any(r.workshop_id == w.id for r in regs): raise HTTPException(400, "Already registered for this workshop")
        if any(r.slot_id == w.slot_id for r in regs): raise HTTPException(400, "Already registered for another workshop in this time slot")
        s.add(WorkshopRegistration(participant_id=p.id, workshop_id=w.id, slot_id=w.slot_id,
                                   attributes={"booking_type": "pre-registered", "attended": False}))
        _log(s, w, "registration", p, current_user)
        try: s.commit()
        except IntegrityError:
            s.rollback(); raise HTTPException(400, "Already registered for this workshop")
        log_audit(current_user, "WORKSHOP_REGISTER", w.workshop_id, {
            "participant_id": p.participant_id,
            "booking_type": "pre-registered",
        })
        return {"message": "Successfully registered for workshop"}


@router.get("/{workshop_id}/seats/stream")
async def stream_workshop_seats(workshop_id: str):
    async def events():
        previous = None
        while True:
            with SessionLocal() as s:
                w = _resolve(s, workshop_id)
                if not w: return
                count = s.scalar(select(func.count()).select_from(WorkshopRegistration).where(
                    WorkshopRegistration.workshop_id == w.id, WorkshopRegistration.status == "registered")) or 0
                payload = {"workshop_id": w.workshop_id, "capacity": w.capacity, "registration_count": count,
                           "available_seats": max(0, w.capacity - count)}
            if payload != previous:
                import json
                yield f"data: {json.dumps(payload)}\n\n"
                previous = payload
            await asyncio.sleep(2)
    return StreamingResponse(events(), media_type="text/event-stream")


@router.post("/{workshop_id}/attendance")
def workshop_attendance(workshop_id: str, request: ScanQRRequest, scan_type: str = "pre-registered", current_user=Depends(get_current_staff)):
    if scan_type not in ("pre-registered", "on-spot"):
        raise HTTPException(400, "Invalid scan_type")
    with SessionLocal() as s:
        w = _resolve(s, workshop_id)
        if not w: raise HTTPException(404, "Workshop not found")
        member = _membership(s, w, current_user.get("paradox_id"))
        if not member: raise HTTPException(403, "Not authorized to scan for this workshop")
        if not member.scanning_enabled: raise HTTPException(403, "Scanning disabled for this volunteer")
        _assert_window(w, scan_type, current_user)
        target_data, payload = verify_qr(request, actor=current_user, domain="workshop", target_id=w.workshop_id)
        p = s.scalar(select(Participant).where(Participant.participant_id == target_data["participant_id"]))
        reg = s.scalar(select(WorkshopRegistration).where(WorkshopRegistration.participant_id == p.id,
            WorkshopRegistration.workshop_id == w.id, WorkshopRegistration.status == "registered"))
        if scan_type == "pre-registered" and not reg:
            raise HTTPException(400, "Participant not pre-registered for this workshop")
        existing = s.scalar(select(WorkshopAttendance).where(WorkshopAttendance.workshop_id == w.id,
            WorkshopAttendance.participant_id == p.id))
        if existing:
            return {"message": "Attendee already marked present"}
        if scan_type == "on-spot":
            used = s.scalar(select(func.count()).select_from(WorkshopAttendance).where(
                WorkshopAttendance.workshop_id == w.id, WorkshopAttendance.scan_type == "on-spot")) or 0
            cap = int(w.capacity * .1)
            if used >= cap: raise HTTPException(400, "Max on-spot capacity (10%) reached")
            held_elsewhere = s.scalar(select(WorkshopRegistration).where(
                WorkshopRegistration.participant_id == p.id,
                WorkshopRegistration.slot_id == w.slot_id,
                WorkshopRegistration.status == "registered",
                WorkshopRegistration.workshop_id != w.id,
            ).with_for_update())
            if held_elsewhere:
                held_elsewhere.status = "cancelled"
                s.flush()
            if reg:
                reg.attributes = {**(reg.attributes or {}), "booking_type": "on-spot", "attended": True}
            else:
                reg = WorkshopRegistration(participant_id=p.id, workshop_id=w.id, slot_id=w.slot_id,
                    attributes={"booking_type": "on-spot", "attended": True})
                s.add(reg)
        s.add(WorkshopAttendance(workshop_id=w.id, participant_id=p.id,
            scanner_id=_staff(s, current_user.get("paradox_id")).id, scan_type=scan_type,
            details={"qr_data": payload if isinstance(payload, dict) else {}}))
        _log(s, w, "attendance", p, current_user, scan_type=scan_type)
        s.commit()
        log_scan(current_user, "workshop", "WORKSHOP_ATTENDANCE", OUTCOME_ALLOWED,
                 participant_id=p.participant_id, target_id=w.workshop_id, details={"scan_type": scan_type})
        return {"message": "On-spot registration successful and marked present" if scan_type == "on-spot" else "Pre-registered attendee marked present"}


@router.get("/{workshop_id}/logs")
def workshop_logs(workshop_id: str, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        _require_admin(s, current_user, "Only Super Admins can view logs")
        w = _resolve(s, workshop_id)
        if not w: raise HTTPException(404, "Workshop not found")
        rows = s.scalars(select(WorkshopLog).where(WorkshopLog.workshop_id == w.id).order_by(WorkshopLog.created_at)).all()
        return {"logs": [{"action": x.action, "participant_id": x.participant_id,
                          "timestamp": x.created_at, **(x.details or {})} for x in rows]}


@router.get("/{workshop_id}/participation")
def workshop_participation(workshop_id: str, current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        w = _resolve(s, workshop_id)
        if not w: raise HTTPException(404, "Workshop not found")
        member = _membership(s, w, current_user.get("paradox_id"))
        admin = _admin(s, current_user.get("paradox_id"))
        if not (admin or member): raise HTTPException(403, "Not authorized to view this workshop's participation")
        regs = s.execute(select(WorkshopRegistration, Participant).join(Participant).where(
            WorkshopRegistration.workshop_id == w.id, WorkshopRegistration.status == "registered")).all()
        rows = []
        for reg, p in regs:
            profile = p.profile or {}
            attended = s.scalar(select(func.count()).select_from(WorkshopAttendance).where(
                WorkshopAttendance.workshop_id == w.id, WorkshopAttendance.participant_id == p.id)) > 0
            rows.append({"participant_id": p.participant_id, "name": p.full_name or profile.get("full_name"),
                "email": p.email, "phone": p.phone or profile.get("phone"), "house": p.house or profile.get("house"),
                "gender": p.gender or profile.get("gender"), "program": p.program or profile.get("program"),
                "course_stage": profile.get("course_stage"), "academic_level": profile.get("academic_level"),
                "academic_level_number": profile.get("academic_level_number"), "degree": profile.get("degree"),
                "entry_year": profile.get("entry_year"), "booking_type": (reg.attributes or {}).get("booking_type", "pre-registered"),
                "attended": attended, "slot_id": _row(s, w).get("slot_id")})
        team = _row(s, w)["workshop_team"]
        return {"workshop_id": w.workshop_id, "name": w.name, "count": len(rows),
                "registration_count": len(rows),
                "attended_count": sum(r["attended"] for r in rows),
                "participant_count": sum(r["attended"] for r in rows),
                "absent_count": sum(not r["attended"] for r in rows),
                "on_spot_count": sum(r["booking_type"] == "on-spot" for r in rows),
                "workshop_team": team, "participants": sorted(rows, key=lambda x: x["participant_id"] or "")}


@router.patch("/{workshop_id}/participants/{participant_id}")
def update_workshop_participant(workshop_id: str, participant_id: str, request: WorkshopParticipantUpdateRequest,
                                current_user=Depends(get_current_staff)):
    with SessionLocal() as s:
        w = _resolve(s, workshop_id)
        if not w: raise HTTPException(404, "Workshop not found")
        member = _membership(s, w, current_user.get("paradox_id")); admin = _admin(s, current_user.get("paradox_id"))
        if not (admin or member): raise HTTPException(403, "Not authorized to update this workshop's participants")
        if not admin and not member.scanning_enabled: raise HTTPException(403, "Scanning disabled for this volunteer")
        _assert_window(w, "changes", current_user)
        if request.attended is None and request.booking_type is None: raise HTTPException(400, "Nothing to update")
        p = s.scalar(select(Participant).where((Participant.participant_id == participant_id) | (Participant.email == participant_id)))
        if not p: raise HTTPException(404, "Participant not found")
        reg = s.scalar(select(WorkshopRegistration).where(WorkshopRegistration.workshop_id == w.id, WorkshopRegistration.participant_id == p.id))
        if not reg: raise HTTPException(404, "Participant is not registered for this workshop")
        attrs = dict(reg.attributes or {})
        if request.booking_type is not None: attrs["booking_type"] = request.booking_type
        if request.attended is not None:
            existing = s.scalar(select(WorkshopAttendance).where(WorkshopAttendance.workshop_id == w.id, WorkshopAttendance.participant_id == p.id))
            if request.attended and not existing:
                s.add(WorkshopAttendance(workshop_id=w.id, participant_id=p.id, scan_type="pre-registered", details={"manual": True}))
            if not request.attended and existing: s.delete(existing)
        reg.attributes = attrs
        _log(s, w, "attendance_override", p, current_user, changes={"attended": request.attended, "booking_type": request.booking_type})
        s.commit()
        log_audit(current_user, "UPDATE_WORKSHOP_PARTICIPANT", w.workshop_id, {"participant_id": p.participant_id})
        return {"message": "Participant record updated", "participant_id": p.participant_id,
                "changes": {k: v for k, v in (("attended", request.attended), ("booking_type", request.booking_type)) if v is not None}}
