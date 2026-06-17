"""PostgreSQL relational schema for Paradox Connect.

The API has a few intentionally flexible JSONB columns (event registration
forms, profile details and audit metadata). Identity, ownership, bookings,
allocations, attendance and staff membership are modelled as ordinary
relational rows with foreign keys and uniqueness constraints.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Participant(Base):
    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_participants_email"),
        Index("ix_participants_house", "house"),
        Index("ix_participants_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="participant")
    full_name: Mapped[str | None] = mapped_column(String(200))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    house: Mapped[str | None] = mapped_column(String(32))
    gender: Mapped[str | None] = mapped_column(String(16))
    phone: Mapped[str | None] = mapped_column(String(32))
    country: Mapped[str | None] = mapped_column(String(80))
    state: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(Text)
    program: Mapped[str | None] = mapped_column(String(8))
    course_stage: Mapped[str | None] = mapped_column(String(24))
    mess_preference: Mapped[str | None] = mapped_column(String(40))
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    qr_secrets: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    embedding: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    mess_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    accommodation_state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    photo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ += (
        CheckConstraint("gender IS NULL OR gender IN ('male','female')", name="ck_participants_gender"),
        CheckConstraint("program IS NULL OR program IN ('DS','MS','AE','ES')", name="ck_participants_program"),
        CheckConstraint("course_stage IS NULL OR course_stage IN ('foundational','diploma','degree')", name="ck_participants_course_stage"),
    )

    event_registrations: Mapped[list["EventRegistration"]] = relationship(back_populates="participant", cascade="all, delete-orphan")
    workshop_registrations: Mapped[list["WorkshopRegistration"]] = relationship(back_populates="participant", cascade="all, delete-orphan")
    mess_allocation: Mapped["MessAllocation | None"] = relationship(back_populates="participant", uselist=False)
    accommodation: Mapped["AccommodationAllocation | None"] = relationship(back_populates="participant", uselist=False)


class StaffUser(Base):
    __tablename__ = "staff_users"
    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_staff_email"),
        UniqueConstraint("participant_id", name="uq_staff_participant_link"),
        CheckConstraint("role IN ('super_admin','admin','other','volunteer')", name="ck_staff_role"),
        Index("ix_staff_role", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    paradox_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    department: Mapped[str | None] = mapped_column(String(80))
    participant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("participants.id", ondelete="SET NULL"))
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Mess(Base):
    __tablename__ = "messes"
    __table_args__ = (
        CheckConstraint("capacity >= 0", name="ck_messes_capacity"),
        CheckConstraint("type IN ('north_indian__veg','north_indian__non_veg','south_indian__veg','south_indian__non_veg','jain')", name="ck_messes_type"),
        Index("ix_messes_type", "type"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mess_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    menu: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    allocations: Mapped[list["MessAllocation"]] = relationship(back_populates="mess")
    staff_members: Mapped[list["MessStaff"]] = relationship(back_populates="mess", cascade="all, delete-orphan")


class Hostel(Base):
    __tablename__ = "hostels"
    __table_args__ = (
        CheckConstraint("capacity >= 0", name="ck_hostels_capacity"),
        CheckConstraint("sharing > 0", name="ck_hostels_sharing"),
        CheckConstraint("current_occupancy >= 0", name="ck_hostels_occupancy"),
        CheckConstraint("gender IN ('male','female','mixed')", name="ck_hostels_gender"),
        Index("ix_hostels_gender", "gender"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostel_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    gender: Mapped[str] = mapped_column(String(16), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sharing: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    current_occupancy: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    rooms: Mapped[list["HostelRoom"]] = relationship(back_populates="hostel", cascade="all, delete-orphan")
    staff_members: Mapped[list["HostelStaff"]] = relationship(back_populates="hostel", cascade="all, delete-orphan")


class HostelRoom(Base):
    __tablename__ = "hostel_rooms"
    __table_args__ = (
        UniqueConstraint("hostel_id", "room_number", name="uq_hostel_room"),
        CheckConstraint("capacity > 0", name="ck_hostel_room_capacity"),
        Index("ix_hostel_rooms_hostel", "hostel_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hostels.id", ondelete="CASCADE"), nullable=False)
    room_number: Mapped[str] = mapped_column(String(32), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    hostel: Mapped[Hostel] = relationship(back_populates="rooms")
    allocations: Mapped[list["AccommodationAllocation"]] = relationship()


class WorkshopSlot(Base):
    __tablename__ = "workshop_slots"
    __table_args__ = (UniqueConstraint("slot_id", name="uq_workshop_slot_id"), CheckConstraint("end_time > start_time", name="ck_workshop_slot_window"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slot_id: Mapped[str] = mapped_column(String(40), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint("capacity IS NULL OR capacity >= 0", name="ck_events_capacity"),
        CheckConstraint("category IN ('technical','culturals','sports','others')", name="ck_events_category"),
        Index("ix_events_category", "category"), Index("ix_events_created_at", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer)
    registration_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    schedule: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    registrations: Mapped[list["EventRegistration"]] = relationship(cascade="all, delete-orphan")
    staff_members: Mapped[list["EventStaff"]] = relationship(cascade="all, delete-orphan")


class Workshop(Base):
    __tablename__ = "workshops"
    __table_args__ = (
        CheckConstraint("capacity >= 0", name="ck_workshops_capacity"),
        Index("ix_workshops_slot", "slot_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workshop_id: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workshop_slots.id", ondelete="RESTRICT"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    registration_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    registrations: Mapped[list["WorkshopRegistration"]] = relationship(cascade="all, delete-orphan")
    staff_members: Mapped[list["WorkshopStaff"]] = relationship(cascade="all, delete-orphan")


class EventRegistration(Base):
    __tablename__ = "event_registrations"
    __table_args__ = (
        UniqueConstraint("participant_id", "event_id", name="uq_event_registration"),
        CheckConstraint("status IN ('registered','cancelled')", name="ck_event_registration_status"),
        Index("ix_event_registrations_event_status", "event_id", "status"),
        Index("ix_event_registrations_event_team", "event_id", "team_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    event_identifier: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    team_id: Mapped[str | None] = mapped_column(ForeignKey("participant_teams.team_id", ondelete="SET NULL"))
    team_identifier: Mapped[str | None] = mapped_column(String(48))
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    participant: Mapped[Participant] = relationship(back_populates="event_registrations")
    event: Mapped[Event] = relationship(back_populates="registrations")


class WorkshopRegistration(Base):
    __tablename__ = "workshop_registrations"
    __table_args__ = (
        CheckConstraint("status IN ('registered','cancelled')", name="ck_workshop_registration_status"),
        Index("ix_workshop_registrations_workshop_status", "workshop_id", "status"),
        Index("uq_workshop_registration_active_workshop", "participant_id", "workshop_id",
              unique=True, postgresql_where=text("status = 'registered'")),
        Index("uq_workshop_registration_active_slot", "participant_id", "slot_id",
              unique=True, postgresql_where=text("status = 'registered'")),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), nullable=False)
    workshop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workshops.id", ondelete="CASCADE"), nullable=False)
    slot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workshop_slots.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="registered")
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    participant: Mapped[Participant] = relationship(back_populates="workshop_registrations")
    workshop: Mapped[Workshop] = relationship(back_populates="registrations")


class EventStaff(Base):
    __tablename__ = "event_staff"
    __table_args__ = (
        UniqueConstraint("staff_id", name="uq_event_staff_one_event"),
        CheckConstraint("role IN ('event_head','member','volunteer')", name="ck_event_staff_role"),
        Index("ix_event_staff_staff", "staff_id"),
    )
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="member")
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    event: Mapped[Event] = relationship(back_populates="staff_members")


class WorkshopStaff(Base):
    __tablename__ = "workshop_staff"
    __table_args__ = (
        CheckConstraint("role IN ('workshop_head','member','volunteer')", name="ck_workshop_staff_role"),
        Index("ix_workshop_staff_staff", "staff_id"),
    )
    workshop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workshops.id", ondelete="CASCADE"), primary_key=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(24), nullable=False, default="member")
    scanning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    workshop: Mapped[Workshop] = relationship(back_populates="staff_members")


class ParticipantTeam(Base):
    __tablename__ = "participant_teams"
    __table_args__ = (
        UniqueConstraint("team_id", name="uq_participant_team_public_id"),
        UniqueConstraint("event_id", "team_id", name="uq_participant_team_event"),
        Index("ix_participant_teams_event", "event_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    team_id: Mapped[str] = mapped_column(String(48), nullable=False)
    leader_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ParticipantTeamMember(Base):
    __tablename__ = "participant_team_members"
    __table_args__ = (CheckConstraint("role IN ('leader','member')", name="ck_participant_team_member_role"), Index("ix_team_members_participant", "participant_id"))
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participant_teams.id", ondelete="CASCADE"), primary_key=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MessAllocation(Base):
    __tablename__ = "mess_allocations"
    __table_args__ = (
        CheckConstraint("status IN ('allocated','cancelled')", name="ck_mess_allocation_status"),
        Index("ix_mess_allocations_mess", "mess_id"),
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True)
    mess_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messes.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="allocated")
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    payment_status: Mapped[str | None] = mapped_column(String(24))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    participant: Mapped[Participant] = relationship(back_populates="mess_allocation")
    mess: Mapped[Mess] = relationship(back_populates="allocations")


class AccommodationAllocation(Base):
    __tablename__ = "accommodation_allocations"
    __table_args__ = (
        CheckConstraint("status IN ('allocated','cancelled')", name="ck_accommodation_allocation_status"),
        Index("ix_accommodation_allocations_room", "room_id"),
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True)
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hostel_rooms.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="allocated")
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    arrival_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    departure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_inside: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_status: Mapped[str | None] = mapped_column(String(24))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    participant: Mapped[Participant] = relationship(back_populates="accommodation")


class MessStaff(Base):
    __tablename__ = "mess_staff"
    __table_args__ = (
        UniqueConstraint("mess_id", "staff_id", name="uq_mess_staff"),
        Index("ix_mess_staff_user", "staff_id"),
        Index("ix_mess_staff_mess", "mess_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mess_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messes.id", ondelete="CASCADE"), nullable=False)
    staff_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    scanning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    mess: Mapped[Mess] = relationship(back_populates="staff_members")


class HostelStaff(Base):
    __tablename__ = "hostel_staff"
    __table_args__ = (
        CheckConstraint("role IN ('hostel_volunteer','guard')", name="ck_hostel_staff_role"),
        Index("ix_hostel_staff_user", "staff_id"),
    )
    hostel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("hostels.id", ondelete="CASCADE"), primary_key=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    scanning_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    hostel: Mapped[Hostel] = relationship(back_populates="staff_members")


class MessAttendance(Base):
    __tablename__ = "mess_attendance"
    __table_args__ = (
        UniqueConstraint("participant_id", "mess_id", "day_number", "meal", name="uq_mess_attendance_service"),
        CheckConstraint("day_number > 0", name="ck_mess_attendance_day"),
        Index("ix_mess_attendance_mess_date", "mess_id", "service_date"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    mess_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messes.id", ondelete="SET NULL"))
    mess_identifier: Mapped[str] = mapped_column(String(40), nullable=False)
    day_number: Mapped[int] = mapped_column(Integer, nullable=False)
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    meal: Mapped[str] = mapped_column(String(24), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    scanner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff_users.id", ondelete="SET NULL"))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_target_time", "target_type", "target_id", "created_at"), Index("ix_audit_actor_time", "actor_id", "created_at"), Index("ix_audit_action_time", "action", "created_at"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SupportQuery(Base):
    __tablename__ = "support_queries"
    __table_args__ = (
        CheckConstraint("category IN ('hostel','mess','event','workshop','general')", name="ck_query_category"),
        CheckConstraint("status IN ('open','assigned','resolved')", name="ck_query_status"),
        CheckConstraint(
            "(category = 'general' AND target_hostel_id IS NULL AND target_mess_id IS NULL AND target_event_id IS NULL AND target_workshop_id IS NULL) OR "
            "(category = 'hostel' AND target_hostel_id IS NOT NULL AND target_mess_id IS NULL AND target_event_id IS NULL AND target_workshop_id IS NULL) OR "
            "(category = 'mess' AND target_hostel_id IS NULL AND target_mess_id IS NOT NULL AND target_event_id IS NULL AND target_workshop_id IS NULL) OR "
            "(category = 'event' AND target_hostel_id IS NULL AND target_mess_id IS NULL AND target_event_id IS NOT NULL AND target_workshop_id IS NULL) OR "
            "(category = 'workshop' AND target_hostel_id IS NULL AND target_mess_id IS NULL AND target_event_id IS NULL AND target_workshop_id IS NOT NULL)",
            name="ck_query_target_matches_category",
        ),
        Index("ix_queries_status_created", "status", "created_at"),
        Index("ix_queries_author", "author_id"),
        Index("ix_queries_assignee_status", "assignee_id", "status"),
        Index("ix_queries_hostel_target", "target_hostel_id"),
        Index("ix_queries_mess_target", "target_mess_id"),
        Index("ix_queries_event_target", "target_event_id"),
        Index("ix_queries_workshop_target", "target_workshop_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    query_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff_users.id", ondelete="SET NULL"))
    target_hostel_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hostels.id", ondelete="RESTRICT"))
    target_mess_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messes.id", ondelete="RESTRICT"))
    target_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"))
    target_workshop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workshops.id", ondelete="RESTRICT"))
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FacilityIssue(Base):
    __tablename__ = "facility_issues"
    __table_args__ = (
        CheckConstraint("facility_type IN ('mess','hostel')", name="ck_issue_facility_type"),
        CheckConstraint("status IN ('open','in_progress','resolved')", name="ck_issue_status"),
        CheckConstraint(
            "(facility_type = 'hostel' AND category IN ('water','electricity','cleanliness','furniture','internet','safety','noise','other')) OR "
            "(facility_type = 'mess' AND category IN ('food_quality','hygiene','service','timing','dietary','other'))",
            name="ck_issue_category_matches_facility",
        ),
        CheckConstraint(
            "(facility_type = 'hostel' AND hostel_ref_id IS NOT NULL AND mess_ref_id IS NULL) OR "
            "(facility_type = 'mess' AND mess_ref_id IS NOT NULL AND hostel_ref_id IS NULL)",
            name="ck_issue_target_matches_type",
        ),
        Index("ix_issues_status_created", "status", "created_at"),
        Index("ix_issues_facility", "facility_type", "facility_id"),
        Index("ix_issues_reporter_status", "reporter_id", "status"),
        Index("ix_issues_hostel_ref", "hostel_ref_id"),
        Index("ix_issues_mess_ref", "mess_ref_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    reporter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    facility_type: Mapped[str] = mapped_column(String(16), nullable=False)
    facility_id: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    hostel_ref_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hostels.id", ondelete="RESTRICT"))
    mess_ref_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messes.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class QueryTeamMember(Base):
    __tablename__ = "query_team_members"
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_users.id", ondelete="CASCADE"), primary_key=True)
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff_users.id", ondelete="SET NULL"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EventAttendance(Base):
    __tablename__ = "event_attendance"
    __table_args__ = (
        UniqueConstraint("event_id", "participant_id", "scanner_id", "service_date", "scan_type", name="uq_event_attendance_scan"),
        Index("ix_event_attendance_time", "event_id", "scanned_at"),
        Index("ix_event_attendance_participant", "participant_id", "scanned_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    event_identifier: Mapped[str] = mapped_column(String(40), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    scanner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff_users.id", ondelete="SET NULL"))
    scan_type: Mapped[str] = mapped_column(String(24), nullable=False)
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class WorkshopAttendance(Base):
    __tablename__ = "workshop_attendance"
    __table_args__ = (UniqueConstraint("workshop_id", "participant_id", "scan_type", name="uq_workshop_attendance_scan"), Index("ix_workshop_attendance_time", "workshop_id", "scanned_at"), Index("ix_workshop_attendance_participant", "participant_id", "scanned_at"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workshop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workshops.id", ondelete="CASCADE"), nullable=False)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    scanner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff_users.id", ondelete="SET NULL"))
    scan_type: Mapped[str] = mapped_column(String(24), nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_payments_amount"),
        CheckConstraint("facility_type IN ('mess','hostel')", name="ck_payments_facility_type"),
        CheckConstraint("status IN ('pending','paid','failed','refunded')", name="ck_payment_status"),
        Index("ix_payments_participant_created", "participant_id", "created_at"),
        UniqueConstraint("transaction_id", name="uq_payment_transaction_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    participant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("participants.id", ondelete="RESTRICT"), nullable=False)
    facility_type: Mapped[str] = mapped_column(String(16), nullable=False)
    facility_id: Mapped[str] = mapped_column(String(40), nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    method: Mapped[str | None] = mapped_column(String(24))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class EventLog(Base):
    __tablename__ = "event_logs"
    __table_args__ = (
        Index("ix_event_logs_event_time", "event_id", "created_at"),
        Index("ix_event_logs_participant_time", "participant_id", "created_at"),
        Index("ix_event_logs_action_time", "action", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"))
    participant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("participants.id", ondelete="SET NULL"))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WorkshopLog(Base):
    __tablename__ = "workshop_logs"
    __table_args__ = (
        Index("ix_workshop_logs_workshop_time", "workshop_id", "created_at"),
        Index("ix_workshop_logs_participant_time", "participant_id", "created_at"),
        Index("ix_workshop_logs_action_time", "action", "created_at"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workshop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workshops.id", ondelete="SET NULL"))
    participant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("participants.id", ondelete="SET NULL"))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class SystemLog(Base):
    __tablename__ = "system_logs"
    __table_args__ = (
        Index("ix_system_logs_action_time", "action", "created_at"),
        Index("ix_system_logs_actor_time", "actor_id", "created_at"),
        Index("ix_system_logs_request", "request_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_name: Mapped[str | None] = mapped_column(String(200))
    actor_type: Mapped[str | None] = mapped_column(String(24))
    actor_role: Mapped[str | None] = mapped_column(String(48))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

