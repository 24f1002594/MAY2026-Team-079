"""
Seed a realistic Paradox student population — profiles, academics, houses,
accommodation, mess, event registrations and workshop registrations.

Run from the ``backend/`` directory. PostgreSQL configuration is read from
``DATABASE_URL`` in the repository-root ``.env`` or process environment:

    python seed_students.py --dry-run              # generate + validate, write nothing
    python seed_students.py                        # write to the configured database
    python seed_students.py --total 800 --seed 7   # smaller, reproducible cohort
    TESTING=1 python seed_students.py --demo-catalogue   # in-memory smoke run

Run it **after** the staff and catalogue seeds, which it hangs its registrations
off::

    python seed_staff.py --bootstrap --roster
    python seed.py --email <admin>          # hostels
    python seed_mess.py --email <admin>     # halls + menus
    python seed_events.py --email <admin>
    python seed_workshops.py --email <admin>
    python seed_staff.py --assign
    python seed_students.py

This script writes the bulk cohort through SQLAlchemy so it can reuse one bcrypt
hash and attach normalized registrations, allocations, attendance, payments and
audit rows in a transaction. The ``seed_source`` marker used by ``--wipe`` is
stored in participant metadata and is never accepted from API requests.

It writes the same document shapes ``POST /auth/register``,
``PATCH /profile/complete``, ``POST /events/{id}/register`` and
``POST /workshops/{id}/register`` produce — checked field for field against
``testing/factories.py``, which mirrors each route — so every endpoint reads the
seeded rows exactly as it reads real ones.

── Timestamps are anchored to now, not to a fixed 2026 calendar ───────────────
Every registration, scan and payment sits just behind the current moment,
because the catalogue's own registration windows do too (see ``seed_calendar``).
The academic calendar — entry years, roll numbers, ages — stays fixed, because
that is a property of the population rather than of when the seed ran.

── Attendance is seeded, not just registration ────────────────────────────────
Workshop attendance (``attended``, ``participant_count``, ``workshop_logs``),
event attendance scans (``event_logs``), meal scans (``mess.scans`` *and* the
``MESS_SCAN`` audit rows the dashboard's meal figure is actually reduced from),
hostel entry state (``inside`` / ``arrival`` / ``departure``) and the mock mess
and hostel fees are all populated, so the organisers' screens have something to
show.

── What "realistic" means here ─────────────────────────────────────────────────
Every distribution is sampled, never laid out. Counts are drawn per cohort with
jitter, so no figure lands on a round number and no two runs agree; house,
gender, degree and activity levels come out close to their target ratios without
being pinned to them. The rules that must hold, hold absolutely:

* Entry years are 2021–2026 only, so every roll number starts 21–26, and 2026
  carries no term-3 intake because that cycle begins after the fest.
* A degree cannot have students who entered before the programme existed.
* Academic level follows from degree and entry year — a 2025 entrant is not at
  BS Degree level, and Management & Data Science / Aeronautics only ever produce
  Foundation Level, because both programmes started in February 2026.
* The roll number encodes entry **year** and entry **term**. The term digit is
  not the academic level; level is a separate field.
* Email domain follows the degree; roll number, email, phone are each unique.
* Name, nationality, location and phone format agree with each other.
* No event or workshop registration falls outside the window the catalogue itself
  advertises — not before it opened, and not in the future. A registration
  outside it is one the API would have refused.

── Where the extra fields live ────────────────────────────────────────────────
``profile`` carries the fourteen keys ``PATCH /profile/complete`` writes plus the
academic record this dataset adds — ``degree``, ``degree_code``, ``degree_type``,
``roll_number``, ``entry_year``, ``entry_term``, ``academic_level``,
``nationality`` and ``age``. Additional profile values remain in the JSONB
profile while core identity fields use indexed relational columns. Two things worth
knowing: the canonical ``program`` / ``course_stage`` pair is always derived from
``degree`` / ``academic_level`` so the admin statistics endpoint keeps working,
and ``PATCH /profile/complete`` replaces ``profile`` wholesale, so a seeded
student who re-submits the profile form loses the added keys (their canonical
fields survive, because the form collects those).

``house`` is a bare name from ``models.HOUSES`` ("Bandipur", not "Bandipur
House"), and ``mess_preference`` is a combined ``{cuisine}__{diet}`` value from
``models.MESS_PREFERENCE_TYPES`` — both closed vocabularies imported from the
model rather than restated here, because a local copy is exactly how the previous
version drifted into writing values the profile validator now rejects.

``mess_preference`` is set only for students who actually took mess, matching the
profile flow where the meal question lives on the Accommodation & Mess step
rather than on Complete Your Profile.

── Demo-data caveats ──────────────────────────────────────────────────────────
* Every student shares one password (``--password``, default ``Paradox@2026``),
  hashed once and reused. 2,000-plus separate bcrypt hashes would take minutes
  and buy nothing for a demo dataset.
* RSA QR keypairs come from a small pool (``--keys``) cycled across the cohort,
  for the same reason. Each student still has a working keypair; several share
  one.
* Photos are public placeholder images (randomuser.me demo portraits, then
  unique DiceBear avatars). None of these people are IIT Madras students.
* Names, addresses and phone numbers are generated. Any resemblance to a real
  person is coincidence.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import delete, func, select, update
from db import SessionLocal
from schema import (AccommodationAllocation, AuditLog, Event, EventAttendance, EventLog,
                    EventRegistration, EventStaff, Hostel, HostelRoom, Mess, MessAllocation,
                    MessAttendance, Participant, ParticipantTeam, ParticipantTeamMember,
                    Payment, StaffUser, SystemLog, Workshop, WorkshopAttendance, WorkshopLog,
                    WorkshopRegistration, WorkshopSlot)
from embedding_service import generate_embedding, zero_embedding
from models import HOUSES, MESS_PREFERENCE_TYPES
from payments import simulate_payment
from security import generate_rsa_key_pair, get_password_hash

import seed_calendar
import seed_students_data as bank


# =============================================================================
# Fest calendar
# =============================================================================
#
# Two different calendars are in play here, and conflating them is what made the
# old version of this script produce a database nobody could test against.
#
# The *academic* calendar — entry years, roll numbers, ages — is a property of
# the student population and stays fixed. A 2023 entrant is a 2023 entrant
# whenever the seed happens to run.
#
# The *activity* calendar — when accounts were created, when events and
# workshops were registered for — has to sit just behind "now", because the
# catalogue's own registration windows are anchored to now (see
# `seed_calendar`). A registration dated May 2026 against a window that opened
# ten days ago is a row the API would never have written.

# The opening day of the fest, which is today. `age` and `dob` are computed "as
# at" this date, so the two can never drift apart depending on when the seed runs.
AGE_REFERENCE = seed_calendar.FEST_DAY_1
FEST_YEAR = AGE_REFERENCE.year

# The activity window. `REGISTRATION_OPENS` matches the catalogue's own window
# exactly, so no seeded registration predates the event or workshop it is for.
REGISTRATION_OPENS = seed_calendar.REGISTRATION_OPENS
# Registrations run right up to the present. Not to `seed_calendar`'s closing
# bound, which is in the future: a student cannot have registered tomorrow.
REGISTRATION_CLOSES = seed_calendar.NOW - timedelta(minutes=1)

# When accounts were created. Sign-ups begin before the activity registration
# window opens, which is why a student's own registrations are always clamped to
# start after both their account and `REGISTRATION_OPENS`.
SIGNUP_OPENS = REGISTRATION_OPENS - timedelta(days=21)
SIGNUP_CLOSES = seed_calendar.NOW - timedelta(hours=1)

# Registrations cluster in the evening, the way a working adult cohort actually
# signs up. Index = hour of day.
HOUR_WEIGHTS: tuple[float, ...] = (
    1.1, 0.7, 0.4, 0.3, 0.3, 0.5,   # 00–05
    0.9, 1.4, 2.0, 2.6, 3.0, 3.1,   # 06–11
    2.8, 2.4, 2.3, 2.5, 2.9, 3.4,   # 12–17
    4.2, 5.1, 6.0, 6.4, 5.2, 3.0,   # 18–23
)


# =============================================================================
# Degrees, cohorts, levels
# =============================================================================


@dataclass(frozen=True)
class Degree:
    """
    One participating degree programme.

    ``code`` is the value written to ``profile.program``, so it stays inside the
    ``DS | ES | AE | MS`` vocabulary the rest of the app uses. ``email_domain``
    is separate because Management & Data Science signs in on ``@mg.…`` while its
    programme code is ``MS`` — which also makes its ``participant_id`` start
    ``MG``, exactly as ``POST /auth/register`` would derive it.

    ``levels`` is the full ladder the programme offers; ``seed_levels`` is the
    subset this dataset is allowed to produce. The two differ for the February
    2026 programmes, which have real Diploma and Degree levels on paper but
    cannot yet have anybody standing on them.
    """

    code: str
    name: str
    email_domain: str
    start_label: str
    start_year: int
    levels: dict[int, str]
    seed_levels: tuple[int, ...]


FOUNDATION = "Foundation Level"
DIPLOMA = "Diploma Level"
BSC_DEGREE = "BSc Degree Level"
BS_DEGREE = "BS Degree Level"

DEGREES: dict[str, Degree] = {
    "DS": Degree(
        code="DS",
        name="BS in Data Science and Applications",
        email_domain="ds.study.iitm.ac.in",
        start_label="2020",
        start_year=2020,
        levels={1: FOUNDATION, 2: DIPLOMA, 3: BSC_DEGREE, 4: BS_DEGREE},
        seed_levels=(1, 2, 3, 4),
    ),
    "ES": Degree(
        code="ES",
        name="BS in Electronic Systems",
        email_domain="es.study.iitm.ac.in",
        start_label="March 2023",
        start_year=2023,
        levels={1: FOUNDATION, 2: DIPLOMA, 3: BS_DEGREE},
        seed_levels=(1, 2, 3),
    ),
    "MS": Degree(
        code="MS",
        name="BS in Management and Data Science",
        email_domain="mg.study.iitm.ac.in",
        start_label="February 2026",
        start_year=2026,
        levels={1: FOUNDATION, 2: DIPLOMA, 3: BS_DEGREE},
        # Foundation only: the programme opened in February 2026, so nobody has
        # had time to clear it yet.
        seed_levels=(1,),
    ),
    "AE": Degree(
        code="AE",
        name="BS in Aeronautics and Space Technology",
        email_domain="ae.study.iitm.ac.in",
        start_label="February 2026",
        start_year=2026,
        levels={1: FOUNDATION, 2: DIPLOMA, 3: BS_DEGREE},
        seed_levels=(1,),
    ),
}

# `profile.course_stage` is the three-value field the app filters and reports on;
# the four academic levels collapse onto it.
LEVEL_TO_STAGE: dict[str, str] = {
    FOUNDATION: "foundational",
    DIPLOMA: "diploma",
    BSC_DEGREE: "degree",
    BS_DEGREE: "degree",
}

# The entry years this dataset is allowed to contain. Data Science opened in
# 2020, but the dataset is deliberately 2021–2026 only, so every roll number
# starts 21–26. `validate_cohort_plan` enforces it against the table below rather
# than trusting the table to stay right.
EARLIEST_ENTRY_YEAR = 2021
LATEST_ENTRY_YEAR = 2026

# Base cohort sizes per (degree, entry year), before jitter. Chosen so the three
# separate requirements in the brief hold at once, which independent marginals
# could not do: the degree split lands near 9:4:2:1, entry years 2021 and 2022
# stay small while 2023–2026 are large, and every Management & Data Science and
# Aeronautics student sits in 2026 because that is when those programmes opened.
BASE_COHORTS: dict[tuple[str, int], int] = {
    ("DS", 2021): 148,
    ("DS", 2022): 171,
    ("DS", 2023): 318,
    ("DS", 2024): 334,
    ("DS", 2025): 327,
    ("DS", 2026): 112,
    ("ES", 2023): 128,
    ("ES", 2024): 151,
    ("ES", 2025): 173,
    ("ES", 2026): 139,
    ("MS", 2026): 298,
    ("AE", 2026): 151,
}

DEFAULT_TOTAL = sum(BASE_COHORTS.values())

# Academic level mix per (degree, entry year), as relative weights.
#
# This is the table that keeps the dataset academically possible. A 2024 entrant
# is mostly at Diploma level; a 2021 entrant is mostly at one of the two Degree
# levels with a few stragglers lower down, because people pause and resume; a
# 2026 entrant can only be at Foundation. Nothing here can produce a level the
# degree does not offer.
LEVEL_MIX: dict[tuple[str, int], dict[str, float]] = {
    ("DS", 2021): {BS_DEGREE: 0.46, BSC_DEGREE: 0.44, DIPLOMA: 0.09, FOUNDATION: 0.01},
    ("DS", 2022): {BS_DEGREE: 0.41, BSC_DEGREE: 0.43, DIPLOMA: 0.14, FOUNDATION: 0.02},
    ("DS", 2023): {DIPLOMA: 0.50, BSC_DEGREE: 0.33, BS_DEGREE: 0.10, FOUNDATION: 0.07},
    ("DS", 2024): {DIPLOMA: 0.72, FOUNDATION: 0.18, BSC_DEGREE: 0.09, BS_DEGREE: 0.01},
    ("DS", 2025): {FOUNDATION: 0.83, DIPLOMA: 0.17},
    ("DS", 2026): {FOUNDATION: 1.0},
    ("ES", 2023): {DIPLOMA: 0.55, BS_DEGREE: 0.34, FOUNDATION: 0.11},
    ("ES", 2024): {DIPLOMA: 0.74, FOUNDATION: 0.21, BS_DEGREE: 0.05},
    ("ES", 2025): {FOUNDATION: 0.84, DIPLOMA: 0.16},
    ("ES", 2026): {FOUNDATION: 1.0},
    ("MS", 2026): {FOUNDATION: 1.0},
    ("AE", 2026): {FOUNDATION: 1.0},
}

# Entry term (the 1/2/3 digit of the roll number) by entry year.
#
# 2026 carries no term-3 intake, by requirement and by fact: that cycle begins
# after the fest, so a 26F3… roll could not exist yet. All three terms appear
# across the dataset as a whole.
#
# Electronic Systems opened in March 2023, so its first year leans away from
# term 1 (see ES_FIRST_YEAR_TERM_MIX).
TERM_MIX: dict[int, dict[int, float]] = {
    2021: {1: 0.39, 2: 0.34, 3: 0.27},
    2022: {1: 0.40, 2: 0.33, 3: 0.27},
    2023: {1: 0.38, 2: 0.34, 3: 0.28},
    2024: {1: 0.39, 2: 0.33, 3: 0.28},
    2025: {1: 0.41, 2: 0.32, 3: 0.27},
    2026: {1: 0.63, 2: 0.37},
}
ES_FIRST_YEAR_TERM_MIX: dict[int, float] = {1: 0.18, 2: 0.46, 3: 0.36}

DEGREE_TYPES: tuple[tuple[str, float], ...] = (
    ("Standalone Degree", 0.63),
    ("Dual Degree", 0.37),
)

# Roll numbers are `YY F <term> <six digits>`. Anchored, because a "generally
# right" roll number is a wrong roll number.
ROLL_PATTERN = re.compile(r"^\d{2}F[123]\d{6}$")

# The twelve IITM BS houses are imported from `models.HOUSES` rather than
# restated here. They used to be a local copy carrying a " House" suffix
# ("Bandipur House"), which `ProfileCompleteRequest` now rejects outright — the
# stored form is bare. A local copy is exactly how that drift happened, so there
# is no longer one: see the import at the top of this file.

# `profile.mess_preference`, validated against `models.MESS_PREFERENCE_TYPES`.
#
# The vocabulary changed shape: the old independent `preference`
# (veg | non_veg | jain) and `cuisines` pair collapsed into one combined
# "{cuisine}__{diet}" value, plus a standalone "jain". A bare "veg" is no longer
# a value a profile may hold.
#
# `POST /mess/allocate` still matches on the dietary half alone (`_diet_of`), so
# the weights below are chosen so the diets they collapse to stay close to the
# old veg/non_veg/jain split — the halls are stocked for that, not for the
# cuisine split.
MESS_PREFERENCES: tuple[tuple[str, float], ...] = (
    ("north_indian__veg", 0.30),
    ("south_indian__veg", 0.25),
    ("north_indian__non_veg", 0.20),
    ("south_indian__non_veg", 0.18),
    ("jain", 0.07),
)

# Share of students who declare a nationality other than Indian. Sized to land
# comfortably under the hundred-student ceiling in the brief at the default
# cohort size, and re-checked after generation regardless.
INTERNATIONAL_TARGET = 0.033
INTERNATIONAL_CEILING = 99

# Age bands as (low, high, weight). Deliberately lopsided: an online BS cohort is
# mostly early twenties with a real, thin tail of working adults and retirees.
AGE_BANDS: tuple[tuple[int, int, float], ...] = (
    (19, 24, 0.56),
    (25, 28, 0.25),
    (29, 40, 0.15),
    (41, 75, 0.04),
)
# Nobody enters a BS programme before finishing school, so a student's current
# age has a floor set by how long ago they joined.
MIN_ENTRY_AGE = 17

# How many events a student registers for, as (low, high, weight). Most of the
# cohort does nothing or one thing; a handful do a dozen.
EVENT_ACTIVITY_TIERS: tuple[tuple[int, int, float], ...] = (
    (0, 0, 0.34),
    (1, 1, 0.22),
    (2, 3, 0.27),
    (4, 6, 0.13),
    (7, 12, 0.04),
)
# Workshops are slot-bound — one per time slot, seven slots in the programme —
# so the ceiling is lower and the zero share higher.
WORKSHOP_ACTIVITY_TIERS: tuple[tuple[int, int, float], ...] = (
    (0, 0, 0.44),
    (1, 1, 0.26),
    (2, 2, 0.17),
    (3, 4, 0.10),
    (5, 7, 0.03),
)

# Which interest theme each degree leans towards when picking activities. The
# lean is a multiplier on top of a random popularity draw, never a filter, so a
# Management student can and does end up at an FPGA workshop.
DEGREE_THEME: dict[str, str] = {
    "DS": "data",
    "ES": "electronics",
    "MS": "management",
    "AE": "aerospace",
}
AFFINITY_BOOST = 1.45          # applied when an item matches the degree's theme
AFFINITY_OFF_THEME_BOOST = 0.2  # small pull towards other technical themes

SEED_MARKER = "seed_students"


# =============================================================================
# Attendance, scans and payments
# =============================================================================
#
# What share of the cohort has turned up, eaten, and paid. Attendance is what the
# organisers' dashboards actually display, and a fest whose every counter reads
# zero tells a tester nothing about whether those screens work.

#: Share of workshop bookings marked as attended.
WORKSHOP_ATTENDED_SHARE = 0.62
#: Share of workshop bookings taken at the door rather than booked ahead. These
#: charge a seat the same way `registration_count` is charged by a pre-booking.
WORKSHOP_ON_SPOT_SHARE = 0.08
#: Share of event registrations with at least one attendance scan against them.
EVENT_SCANNED_SHARE = 0.55
#: Share of allotted diners who have scanned a meal, and the share of that
#: hall's *past* sittings each of them scanned.
MESS_SCANNED_SHARE = 0.7
MESS_SITTING_SCANNED_SHARE = 0.75
#: Share of residents currently inside their block, and the share who have
#: checked out for good.
HOSTEL_INSIDE_SHARE = 0.33
HOSTEL_DEPARTED_SHARE = 0.06
#: Share of those who requested mess / a room who have settled the mock fee.
MESS_PAID_SHARE = 0.68
HOSTEL_PAID_SHARE = 0.74

#: Team ids are minted here rather than taken from a name bank.
#: `participants.events[].team_id` holds a backend-assigned id
#: (`TM<TYPE><counter>` from `EventIDGenerator.next_team_id`), never a display
#: name — the old version of this script stored "Cosmic Falcons" there, which no
#: route would ever have written and which `PUT
#: /events/{id}/participant_teams/{id}` would not recognise.
#:
#: The counter starts far above the generator's own 111111 because that counter
#: is in-memory and restarts on every process restart, so a low seeded id would
#: be handed out again by the first team a real participant creates.
TEAM_ID_SEQUENCE_BASE = 900_001


# =============================================================================
# Small helpers
# =============================================================================


def weighted_pick(rng: random.Random, choices: dict[Any, float]) -> Any:
    """One key from ``{key: weight}``, chosen in proportion to the weights."""
    keys = list(choices)
    return rng.choices(keys, weights=[choices[k] for k in keys], k=1)[0]


def pick_pair(rng: random.Random, pairs: Sequence[tuple[Any, float]]) -> Any:
    """One value from ``[(value, weight), …]``."""
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs], k=1)[0]


def jitter(rng: random.Random, value: int, spread: float = 0.09) -> int:
    """
    ``value`` nudged by up to ±``spread``, never below 1.

    Applied to every planned cohort size so the dataset never shows a figure that
    was typed rather than sampled.
    """
    return max(1, int(round(value * rng.uniform(1 - spread, 1 + spread))))


def fill_digits(rng: random.Random, pattern: str) -> str:
    """``'8### ####'`` → ``'8241 9073'``. ``#`` becomes a random digit."""
    return "".join(str(rng.randrange(10)) if ch == "#" else ch for ch in pattern)


def dob_for_age(rng: random.Random, age: int) -> str:
    """A birth date that makes the student exactly ``age`` on the fest's opening day."""
    latest = date(AGE_REFERENCE.year - age, AGE_REFERENCE.month, AGE_REFERENCE.day)
    earliest = date(AGE_REFERENCE.year - age - 1, AGE_REFERENCE.month, AGE_REFERENCE.day) + timedelta(days=1)
    return (earliest + timedelta(days=rng.randrange((latest - earliest).days + 1))).isoformat()


def timestamp_between(rng: random.Random, earliest: datetime, latest: datetime) -> datetime:
    """
    A moment in ``[earliest, latest]``, front-loaded across the days and skewed
    towards the evening within a day.

    Two shapes rather than one uniform draw, because that is what a registration
    window actually looks like: a rush when it opens, a long tail afterwards, and
    almost nobody signing up at 04:00.
    """
    if latest <= earliest:
        return earliest
    total_days = (latest.date() - earliest.date()).days
    offset = min(total_days, int(rng.betavariate(1.7, 3.0) * (total_days + 1)))
    day = earliest.date() + timedelta(days=offset)
    hour = rng.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
    stamp = datetime(
        day.year, day.month, day.day, hour,
        rng.randrange(60), rng.randrange(60), rng.randrange(1000) * 1000,
        tzinfo=earliest.tzinfo,
    )
    if stamp < earliest:
        span = max(1, int((latest - earliest).total_seconds()))
        stamp = earliest + timedelta(seconds=rng.randrange(min(span, 36 * 3600)))
    if stamp > latest:
        span = max(1, int((latest - earliest).total_seconds()))
        stamp = latest - timedelta(seconds=rng.randrange(min(span, 36 * 3600)))
    return stamp


def weighted_sample(
    rng: random.Random, items: Sequence[Any], weights: Sequence[float]
) -> list[Any]:
    """
    ``items`` ordered by a weighted draw without replacement.

    The exponential-race trick: sort by ``-log(U)/w``. Returning the whole
    ordering rather than the first *k* lets the caller keep walking down the list
    when a pick has to be skipped — a workshop whose slot is already taken, or
    one that has just filled up.
    """
    keyed = [
        (-math.log(max(rng.random(), 1e-12)) / max(w, 1e-9), i)
        for i, w in enumerate(weights)
    ]
    keyed.sort()
    return [items[i] for _, i in keyed]


def affinity_themes(text: str) -> set[str]:
    """Which interest themes an event or workshop blurb matches."""
    lowered = f" {text.lower()} "
    return {
        theme
        for theme, keywords in bank.AFFINITY_KEYWORDS.items()
        if any(keyword in lowered for keyword in keywords)
    }


def diet_of(mess_type: str) -> str:
    """
    The dietary axis of a hall's combined ``type``, e.g. ``"north_indian__veg"``
    -> ``"veg"``.

    Mirrors ``routers.mess._diet_of`` rather than importing it, for the reason
    ``participant_id_for`` mirrors ``main.generate_participant_id``: importing
    the router would pull FastAPI and every dependency into a script that needs
    one line of string handling. This is what lets a student's combined
    preference be matched against a hall the same way ``POST /mess/allocate``
    matches it.
    """
    if mess_type == "jain":
        return "jain"
    return (mess_type or "").rsplit("__", 1)[-1]


def mint_team_id(event_type: str, sequence: int) -> str:
    """
    A team id in the form ``EventIDGenerator.next_team_id`` produces:
    ``"TM" + event_type[:3].upper() + counter``.

    Mirrored rather than imported so the counter can start from this script's own
    reserved base — the generator's instance counter is in-memory and would
    restart at 111111 in the API process, handing out ids this script had
    already used.
    """
    return f"TM{(event_type or 'others')[:3].upper()}{sequence}"


# =============================================================================
# Generated student
# =============================================================================


@dataclass
class Student:
    """A participant document under construction, plus the facts needed to build it."""

    degree: Degree
    entry_year: int
    entry_term: int
    level_number: int
    level_name: str
    roll_number: str
    email: str
    participant_id: str
    full_name: str
    gender: str
    age: int
    dob: str
    nationality: str
    country: str
    state: str
    city: str
    address: str
    phone: str
    house: str
    degree_type: str
    photo: str
    photo_kind: str
    event_preferences: str
    emergency_contact: dict[str, str]
    created_at: datetime

    # Filled in by the later passes.
    stay: str = "neither"                     # both | accommodation | mess | neither
    mess_preference: str | None = None
    hostel_id: str | None = None
    room: str | None = None
    # Which room of that block, by index. `POST /hostels/allocate` writes
    # `rooms.{index}.occupants`, so the mirror pass needs the index and not only
    # the room number.
    room_index: int | None = None
    # `accommodation.inside` / `arrival` / `departure`, as a hostel entry scan
    # would have left them. `logged_in` used to stand in for `inside`; that field
    # does not exist on the participant document.
    inside: bool = False
    arrival: datetime | None = None
    departure: datetime | None = None
    mess_oid: Any = None
    # `mess.scans[day_key][slot]`, in the shape `scan_mess` writes.
    mess_scans: dict[str, dict[str, Any]] = field(default_factory=dict)
    mess_payment: dict[str, Any] | None = None
    accommodation_payment: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    workshops: list[dict[str, Any]] = field(default_factory=list)
    event_logs: list[tuple[Any, datetime]] = field(default_factory=list)
    workshop_logs: list[tuple[Any, datetime]] = field(default_factory=list)
    # Attendance scans, as `(event_oid, scanned_by, when)` and
    # `(workshop_oid, when)` — written to `event_logs` / `workshop_logs` and, for
    # mess, to the audit trail.
    event_scans: list[tuple[Any, str, datetime]] = field(default_factory=list)
    workshop_attendance: list[tuple[Any, datetime]] = field(default_factory=list)
    mess_scan_rows: list[tuple[str, str, int, datetime]] = field(default_factory=list)


# =============================================================================
# Cohort planning
# =============================================================================


def validate_cohort_plan() -> None:
    """
    Check the tables above before a single student is generated.

    These are the two rules easiest to reintroduce by editing a table: an entry
    year outside 2021–2026, and a 2026 term-3 intake that cannot exist yet. Both
    are also checked per student in the report, but failing here means a bad table
    never reaches the database in the first place.
    """
    years = {year for _, year in BASE_COHORTS}
    stray = sorted(y for y in years if not EARLIEST_ENTRY_YEAR <= y <= LATEST_ENTRY_YEAR)
    if stray:
        raise SystemExit(
            f"BASE_COHORTS has entry years outside "
            f"{EARLIEST_ENTRY_YEAR}–{LATEST_ENTRY_YEAR}: {stray}"
        )
    if 3 in TERM_MIX.get(2026, {}):
        raise SystemExit("TERM_MIX[2026] offers term 3, which does not exist yet")
    missing = years - set(TERM_MIX)
    if missing:
        raise SystemExit(f"TERM_MIX has no entry for {sorted(missing)}")
    for key in BASE_COHORTS:
        if key not in LEVEL_MIX:
            raise SystemExit(f"LEVEL_MIX has no entry for {key}")


def plan_cohorts(rng: random.Random, total: int | None) -> list[tuple[str, int, int]]:
    """
    ``[(degree_code, entry_year, count), …]`` — the shape of the population.

    ``total`` is a target, not a quota: the plan is scaled towards it and then
    every cell is jittered, so the realised total lands near the request without
    being a round number. Cells never drop below one student, which keeps the
    smallest cohorts present at small ``--total`` values.
    """
    scale = 1.0 if total is None else total / DEFAULT_TOTAL
    plan: list[tuple[str, int, int]] = []
    for (code, year), base in BASE_COHORTS.items():
        scaled = max(1, int(round(base * scale)))
        plan.append((code, year, jitter(rng, scaled)))
    return plan


def pick_level(rng: random.Random, code: str, year: int) -> tuple[int, str]:
    """
    The academic level for one student, as ``(level number, level name)``.

    Sampled from ``LEVEL_MIX`` and then checked against the degree's own ladder,
    so a mix entry that named a level the degree does not offer could never leak
    into the data — it would raise here instead.
    """
    degree = DEGREES[code]
    mix = LEVEL_MIX[(code, year)]
    name = weighted_pick(rng, mix)
    numbers = {label: number for number, label in degree.levels.items()}
    number = numbers[name]
    if number not in degree.seed_levels:
        raise AssertionError(f"{degree.name} cannot seed {name!r}")
    return number, name


def pick_term(rng: random.Random, code: str, year: int) -> int:
    """The entry term digit — 1, 2 or 3. Not the academic level."""
    if code == "ES" and year == 2023:
        return weighted_pick(rng, ES_FIRST_YEAR_TERM_MIX)
    return weighted_pick(rng, TERM_MIX[year])


# =============================================================================
# Identity
# =============================================================================


class PhotoPool:
    """
    Hands out a distinct photo per student.

    The 100 men's and 100 women's randomuser.me demo portraits go first, each
    used at most once and matched to gender; everybody after that gets a DiceBear
    avatar seeded on their roll number, which is unique by construction. The
    result is one image per student with no reuse at all.
    """

    def __init__(self, rng: random.Random) -> None:
        self._pools = {
            "male": list(range(bank.RANDOMUSER_PORTRAITS_PER_GENDER)),
            "female": list(range(bank.RANDOMUSER_PORTRAITS_PER_GENDER)),
        }
        for pool in self._pools.values():
            rng.shuffle(pool)

    def take(self, rng: random.Random, gender: str, roll_number: str) -> tuple[str, str]:
        pool = self._pools.get(gender)
        if pool:
            group = "men" if gender == "male" else "women"
            return bank.RANDOMUSER_URL.format(group=group, index=pool.pop()), "portrait"
        style = rng.choice(bank.DICEBEAR_STYLES)
        return bank.DICEBEAR_URL.format(style=style, seed=roll_number), "avatar"


def participant_id_for(email: str) -> str:
    """
    The participant id ``POST /auth/register`` would derive from this email.

    Mirrors ``main.generate_participant_id`` (``backend/main.py``) rather than
    importing it, so seeding does not pull the whole FastAPI application — and
    the derivation is asserted against the roll number during validation. Note
    Management & Data Science signs in on ``@mg.…``, so its ids start ``MG``
    while its programme code is ``MS``; that is the backend's own behaviour.
    """
    match = re.match(r"^([^@]+)@([a-z]+)\.study\.iitm\.ac\.in$", email.lower())
    if match:
        return f"{match.group(2).upper()}{match.group(1).upper()}"
    return email.split("@")[0].upper()


def make_roll_number(rng: random.Random, year: int, term: int, used: set[str]) -> str:
    """A unique ``YYF<term><six digits>`` roll number."""
    while True:
        roll = f"{year % 100:02d}F{term}{rng.randrange(1000, 1000000):06d}"
        if roll not in used:
            used.add(roll)
            return roll


def make_phone(
    rng: random.Random, dial_code: str | None, patterns: Sequence[str], used: set[str]
) -> str:
    """
    A unique contact number in the right national shape.

    Indian numbers are stored as bare ten digits, which is both how an Indian
    student writes theirs and what the profile form validates. Everyone else
    keeps their dialling code, so the number reads correctly for where they live.
    """
    while True:
        national = fill_digits(rng, rng.choice(patterns))
        phone = national if dial_code is None else f"{dial_code} {national}"
        if phone not in used:
            used.add(phone)
            return phone


def make_indian_identity(rng: random.Random, gender: str) -> dict[str, str]:
    """Name, nationality and address for an Indian student, drawn from one region."""
    region = rng.choices(
        bank.INDIAN_REGIONS, weights=[r.weight for r in bank.INDIAN_REGIONS], k=1
    )[0]
    first = rng.choice(region.male if gender == "male" else region.female)
    surname = rng.choice(region.surnames)
    state, city = rng.choice(region.places)
    return {
        "full_name": f"{first} {surname}",
        "surname": surname,
        "names": region,
        "nationality": "Indian",
        "country": "India",
        "state": state,
        "city": city,
        "address": f"{rng.randrange(1, 240)}, {rng.choice(bank.INDIAN_STREETS)}",
    }


def make_international_identity(rng: random.Random, gender: str) -> dict[str, Any]:
    """
    Name, nationality, address and phone shape for a student living abroad.

    Gulf entries carry an ``indian_expat_share``: a good part of the IITM BS
    cohort in the UAE, Kuwait, Oman and Bahrain is Indian expatriate, so those
    students get an Indian name and nationality with a local address and a local
    number. Name and nationality still agree — which is the rule — they just do
    not agree with the country of residence, exactly as in life.
    """
    country = rng.choices(
        bank.INTERNATIONAL_COUNTRIES,
        weights=[c.weight for c in bank.INTERNATIONAL_COUNTRIES],
        k=1,
    )[0]
    state, city = rng.choice(country.names.places)
    expat = rng.random() < country.indian_expat_share
    if expat:
        indian = make_indian_identity(rng, gender)
        full_name, nationality = indian["full_name"], "Indian"
        surname, names = indian["surname"], indian["names"]
    else:
        first = rng.choice(country.names.male if gender == "male" else country.names.female)
        # Kept as its own value rather than split back off the full name: Gulf
        # surnames are two words ("Al Marzooqi"), so splitting on the last space
        # would hand back "Marzooqi" — a surname that is in no name bank.
        surname = rng.choice(country.names.surnames)
        full_name = f"{first} {surname}"
        nationality = country.nationality
        names = country.names
    return {
        "full_name": full_name,
        "surname": surname,
        "names": names,
        "nationality": nationality,
        "country": country.country,
        "state": state,
        "city": city,
        "address": f"{rng.randrange(1, 180)} {rng.choice(bank.INTERNATIONAL_STREETS)}",
        "dial_code": country.dial_code,
        "phone_patterns": country.phone_patterns,
        "region": country.region,
        "expat": expat,
    }


def make_event_preferences(rng: random.Random, code: str) -> str:
    """
    The free-text interests line, which the backend embeds for recommendations.

    Two or three phrases: at least one from the degree's own theme, the rest from
    anywhere. That is what stops every Data Science student writing the same
    sentence while still leaving the embedding something degree-shaped to work
    with.
    """
    weights = bank.DEGREE_INTEREST_WEIGHTS[code]
    themes: list[str] = []
    for _ in range(rng.choice((2, 2, 3))):
        theme = weighted_pick(rng, weights)
        if theme not in themes:
            themes.append(theme)
    phrases = [rng.choice(bank.INTEREST_PHRASES[theme]) for theme in themes]
    joiner = rng.choice((". ", "; ", ", and "))
    text = joiner.join(phrases)
    return text[0].upper() + text[1:]


def make_emergency_contact(
    rng: random.Random, surname: str, gender_bank: dict[str, Any], dial_code: str | None,
    patterns: Sequence[str], used: set[str]
) -> dict[str, str]:
    """A next-of-kin whose name and number match the student's own family and country."""
    relation = pick_pair(rng, bank.EMERGENCY_RELATIONS)
    pool = gender_bank["male"] if relation in ("father", "guardian") else gender_bank["female"]
    if relation == "elder_sibling":
        pool = gender_bank[rng.choice(("male", "female"))]
    return {
        "name": f"{rng.choice(pool)} {surname}",
        "relation": relation,
        "phone": make_phone(rng, dial_code, patterns, used),
    }


def make_student(
    rng: random.Random,
    code: str,
    year: int,
    photos: PhotoPool,
    used_rolls: set[str],
    used_emails: set[str],
    used_phones: set[str],
    house_weights: dict[str, float],
    international: bool,
) -> Student:
    """One complete, internally consistent student."""
    degree = DEGREES[code]

    # Roughly 3:2 male:female, sampled per student rather than allotted, so the
    # realised split lands near the ratio without matching it.
    gender = "male" if rng.random() < 0.6 else "female"

    term = pick_term(rng, code, year)
    level_number, level_name = pick_level(rng, code, year)
    roll = make_roll_number(rng, year, term, used_rolls)

    email = f"{roll.lower()}@{degree.email_domain}"
    if email in used_emails:  # only reachable if the collection already holds this roll
        return make_student(
            rng, code, year, photos, used_rolls, used_emails, used_phones,
            house_weights, international,
        )
    used_emails.add(email)

    if international:
        identity = make_international_identity(rng, gender)
        dial_code = identity["dial_code"]
        patterns = identity["phone_patterns"]
        name_bank_for_kin = _kin_bank_for(identity)
    else:
        identity = make_indian_identity(rng, gender)
        dial_code = None
        patterns = bank.INDIA_MOBILE_PATTERNS
        name_bank_for_kin = _kin_bank_for(identity)

    # Age has a hard floor: nobody starts a BS programme before finishing school,
    # so somebody who joined in 2021 cannot be 19 now. Resampled rather than
    # clamped, so the band weights still shape what comes out.
    floor = max(19, MIN_ENTRY_AGE + (FEST_YEAR - year))
    age = _sample_age(rng, floor)

    phone = make_phone(rng, dial_code, patterns, used_phones)
    photo, photo_kind = photos.take(rng, gender, roll)

    return Student(
        degree=degree,
        entry_year=year,
        entry_term=term,
        level_number=level_number,
        level_name=level_name,
        roll_number=roll,
        email=email,
        participant_id=participant_id_for(email),
        full_name=identity["full_name"],
        gender=gender,
        age=age,
        dob=dob_for_age(rng, age),
        nationality=identity["nationality"],
        country=identity["country"],
        state=identity["state"],
        city=identity["city"],
        address=identity["address"],
        phone=phone,
        house=weighted_pick(rng, house_weights),
        degree_type=pick_pair(rng, DEGREE_TYPES),
        photo=photo,
        photo_kind=photo_kind,
        event_preferences=make_event_preferences(rng, code),
        emergency_contact=make_emergency_contact(
            rng, identity["surname"], name_bank_for_kin, dial_code, patterns, used_phones
        ),
        created_at=timestamp_between(rng, SIGNUP_OPENS, SIGNUP_CLOSES),
    )


def _kin_bank_for(identity: dict[str, Any]) -> dict[str, Any]:
    """
    First-name pools for the student's family — the bank their own name came
    from, so an emergency contact never reads as a different family.

    Carried through on the identity rather than found by searching the banks for
    the student's surname. The search version looked right and was quietly wrong:
    a Gulf student's surname is two words, the lookup missed, and the silent
    fallback gave "Shaikha Al Marzooqi" a next of kin called "Muskan Marzooqi".
    """
    names: bank.NameBank = identity["names"]
    return {"male": names.male, "female": names.female}


def _sample_age(rng: random.Random, floor: int) -> int:
    """An age from the weighted bands, at or above ``floor``."""
    for _ in range(60):
        low, high, _weight = pick_pair(
            rng, tuple((band, band[2]) for band in AGE_BANDS)
        )
        age = rng.randint(low, high)
        if age >= floor:
            return age
    return floor + rng.randrange(4)


def build_population(
    rng: random.Random, total: int | None, existing: dict[str, set[str]]
) -> list[Student]:
    """Every student, in a shuffled order so later passes are not cohort-ordered."""
    used_rolls = set(existing["rolls"])
    used_emails = set(existing["emails"])
    used_phones = set(existing["phones"])

    photos = PhotoPool(rng)
    # Per-run house weights, so the twelve houses come out unbalanced in a
    # different direction every time rather than near-equal every time.
    house_weights = {house: rng.uniform(0.78, 1.28) for house in HOUSES}

    plan = plan_cohorts(rng, total)
    slots = [(code, year) for code, year, count in plan for _ in range(count)]

    # How many students live abroad, drawn once for the whole cohort and then
    # dealt out to named slots. Sampling it per student instead — a coin flip
    # weighted to land near the target on average — left the realised count free
    # to wander a couple of standard deviations above its mean, which put a few
    # runs in every hundred over the ceiling the brief sets. Here the ceiling is
    # a property of the dataset, not of the expected value, and the count is
    # still drawn rather than typed.
    wanted = int(round(len(slots) * INTERNATIONAL_TARGET * rng.uniform(0.82, 1.16)))
    abroad = set(rng.sample(range(len(slots)), min(wanted, INTERNATIONAL_CEILING, len(slots))))

    students = [
        make_student(
            rng, code, year, photos, used_rolls, used_emails, used_phones,
            house_weights, index in abroad,
        )
        for index, (code, year) in enumerate(slots)
    ]
    rng.shuffle(students)
    return students


# =============================================================================
# Accommodation and mess
# =============================================================================


def assign_stay(rng: random.Random, students: list[Student]) -> None:
    """
    Split the cohort across both / accommodation only / mess only / neither.

    The brief fixes the ordering — both > accommodation only > mess only — and
    asks for counts that look drawn rather than chosen, so the three sizes are
    sampled from overlapping-but-ordered ranges and then asserted. Everyone else
    is commuting and eating off campus, which is most of an online cohort.
    """
    total = len(students)
    scale = total / DEFAULT_TOTAL

    def sized(low: int, high: int) -> int:
        return max(3, int(round(rng.randint(low, high) * scale)))

    # The three groups together never claim more of the cohort than this, which
    # is what leaves a majority commuting. The floor of 6 is what makes the
    # smallest runs work: three groups in a strict order need at least 3 + 2 + 1.
    cap = max(6, int(total * 0.58))

    # Each size is drawn, then clamped so the brief's ordering holds by
    # construction — both > accommodation only > mess only — and so the three
    # still fit inside the cap. Clamping beats the shrink loop that was here
    # before: repeatedly scaling all three by 0.9 could round two of them onto
    # the same value, and at small `--total` the floors alone collapsed the top
    # two to 3 and 3, tripping the assertion below rather than seeding anything.
    both = min(sized(318, 361), cap - 3)
    accommodation = min(sized(254, 303), both - 1, cap - both - 1)
    mess = min(sized(181, 247), accommodation - 1, cap - both - accommodation)

    assert both > accommodation > mess >= 1, (both, accommodation, mess)

    pool = students[:]
    rng.shuffle(pool)
    cursor = 0
    for choice, count in (("both", both), ("accommodation", accommodation), ("mess", mess)):
        for student in pool[cursor:cursor + count]:
            student.stay = choice
        cursor += count

    # The meal preference is only asked of students who actually take mess — the
    # question lives on the Accommodation & Mess step, not on the profile form.
    for student in students:
        if student.stay in ("both", "mess"):
            student.mess_preference = pick_pair(rng, MESS_PREFERENCES)


def allocate_hostels(rng: random.Random, students: list[Student], hostels: list[dict]) -> int:
    """
    Place most accommodation requests into a real room of a gender-matched block.

    A slice is left unplaced on purpose. ``POST /hostels/allocate`` selects on
    ``{"accommodation.registered": True, "accommodation.hostel_id": None}``, so
    those students are a live queue for the organisers' batch — and the
    "awaiting allocation" state on the student's own Stay screen has real data
    behind it.

    Two things this now matches that it did not before. Rooms are taken from the
    block's own ``rooms`` array, filling each up to its ``sharing`` limit, rather
    than the room number being invented from a running count — so the seeded
    ``accommodation.room`` is a room that exists, and the occupants can be
    mirrored onto the block. And the ceiling is
    ``min(capacity, sharing * len(rooms))``, which is exactly what
    ``allocate_hostels`` enforces: a block created with more beds than capacity
    still only takes ``capacity`` residents.
    """
    by_gender: dict[str, list[dict]] = defaultdict(list)
    for hostel in hostels:
        by_gender[str(hostel.get("gender", "")).lower()].append(hostel)
    for blocks in by_gender.values():
        blocks.sort(key=lambda h: h.get("hostel_id", ""))

    # Live room occupancy per block, seeded from whatever is already stored so a
    # re-run cannot double-fill a room.
    room_fill: dict[str, list[int]] = {
        h["hostel_id"]: [len(room.get("occupants") or []) for room in (h.get("rooms") or [])]
        for h in hostels
    }
    ceiling = {
        h["hostel_id"]: min(
            int(h.get("capacity", 0) or 0),
            int(h.get("sharing", 1) or 1) * len(h.get("rooms") or []),
        )
        for h in hostels
    }
    occupancy: Counter = Counter(
        {h["hostel_id"]: int(h.get("current_occupancy", 0) or 0) for h in hostels}
    )

    placed = 0
    waiting = [s for s in students if s.stay in ("both", "accommodation")]
    rng.shuffle(waiting)
    # Between roughly a sixth and a fifth stay in the queue.
    to_place = int(len(waiting) * rng.uniform(0.79, 0.87))

    for student in waiting[:to_place]:
        blocks = [
            b for b in by_gender.get(student.gender, [])
            if occupancy[b["hostel_id"]] < ceiling[b["hostel_id"]]
        ]
        if not blocks:
            continue
        # Weighted by remaining space, so blocks fill unevenly but nothing
        # overflows.
        block = rng.choices(
            blocks,
            weights=[ceiling[b["hostel_id"]] - occupancy[b["hostel_id"]] for b in blocks],
            k=1,
        )[0]

        hostel_id = block["hostel_id"]
        sharing = int(block.get("sharing", 1) or 1)
        rooms = block.get("rooms") or []
        room_index = next(
            (i for i, taken in enumerate(room_fill[hostel_id]) if taken < sharing), None
        )
        if room_index is None:
            continue

        room_fill[hostel_id][room_index] += 1
        occupancy[hostel_id] += 1
        student.hostel_id = hostel_id
        student.room_index = room_index
        student.room = str(rooms[room_index].get("room_number"))

        # Arrival, and whether they are inside right now, as entry/exit scans
        # would have left them. `departure` is only ever set on somebody who has
        # been inside, and it implies they are not inside now — the same
        # invariant `scan_hostel` enforces.
        student.arrival = timestamp_between(rng, REGISTRATION_OPENS, REGISTRATION_CLOSES)
        if rng.random() < HOSTEL_DEPARTED_SHARE:
            student.departure = timestamp_between(rng, student.arrival, REGISTRATION_CLOSES)
            student.inside = False
        else:
            student.inside = rng.random() < HOSTEL_INSIDE_SHARE
        placed += 1
    return placed


def allocate_mess(rng: random.Random, students: list[Student], halls: list[dict]) -> int:
    """
    Place most mess requests into a hall that serves what they eat.

    ``POST /mess/allocate`` buckets halls by the dietary axis of their ``type``
    (``mess._diet_of``) and matches a student's ``profile.mess_preference``
    through the same collapse, so the pairing here is the one the batch would
    make. The old version grouped on a ``preference`` field the hall documents no
    longer have, which meant every bucket was empty and nobody was ever placed.

    As with hostels, a slice is left unplaced so the organisers' batch has a
    queue.
    """
    by_diet: dict[str, list[dict]] = defaultdict(list)
    for hall in halls:
        by_diet[diet_of(str(hall.get("type", "")))].append(hall)

    occupancy: Counter = Counter({str(h["_id"]): int(h.get("current_occupancy", 0) or 0) for h in halls})
    placed = 0
    waiting = [s for s in students if s.stay in ("both", "mess")]
    rng.shuffle(waiting)
    to_place = int(len(waiting) * rng.uniform(0.84, 0.91))

    for student in waiting[:to_place]:
        options = [
            h for h in by_diet.get(diet_of(student.mess_preference or ""), [])
            if occupancy[str(h["_id"])] < int(h.get("capacity", 0))
        ]
        if not options:
            continue
        hall = rng.choice(options)
        occupancy[str(hall["_id"])] += 1
        # The allocation pass holds the hall's relational UUID internally;
        # account projections return the readable mess identifier to clients.
        student.mess_oid = hall["_id"]
        placed += 1
    return placed


def assign_mess_scans(rng: random.Random, students: list[Student], halls: list[dict]) -> int:
    """
    Give most allotted diners a scan history against their hall's own menu.

    ``mess.scans`` is keyed exactly as the hall's ``menu`` is — ``day_1``,
    ``day_2``, ... each holding ``breakfast`` / ``lunch`` / ``dinner`` — and
    ``GET /mess/my_mess`` derives its whole display list by merging the two, so a
    scan against a sitting the hall does not serve would simply not appear.

    Only sittings that have already *finished* are marked. A scan recorded
    against tomorrow's dinner is a row no scanner could have produced, and it
    would make the meal counts on the dashboard exceed the meals actually
    served.
    """
    halls_by_oid = {h["_id"]: h for h in halls}
    scanned = 0

    for student in students:
        if not student.mess_oid or rng.random() > MESS_SCANNED_SHARE:
            continue
        hall = halls_by_oid.get(student.mess_oid)
        if hall is None:
            continue

        mess_id = str(hall.get("mess_id"))
        for day_key, sittings in (hall.get("menu") or {}).items():
            for slot, sitting in (sittings or {}).items():
                end = sitting.get("end_time")
                if not isinstance(end, datetime) or end >= seed_calendar.NOW:
                    continue
                if rng.random() > MESS_SITTING_SCANNED_SHARE:
                    continue
                start = sitting.get("start_time") or end
                when = timestamp_between(rng, start, end)
                student.mess_scans.setdefault(day_key, {})[slot] = {
                    "scanned": True,
                    "scanned_at": when,
                }
                # The audit row is what the dashboard's meal figure is actually
                # reduced from — see `audit._meal_summary`, which counts distinct
                # diners out of `MESS_SCAN` rows rather than reading `mess.scans`.
                student.mess_scan_rows.append((mess_id, slot, int(day_key.split("_")[1]), when))
                scanned += 1

    return scanned


def assign_payments(rng: random.Random, students: list[Student]) -> tuple[int, int]:
    """
    Settle the mock mess and hostel fees for most of those who asked for them.

    Uses ``payments.simulate_payment``, the same function ``POST /mess/pay`` and
    ``POST /hostels/pay`` call, so the stored receipt is byte-for-byte the shape
    those routes write — including the ``PDX-MESS-…`` / ``PDX-HOSTEL-…``
    transaction id prefix a support conversation would search for.

    Deliberately independent of allocation, exactly as the routes are: paying
    does not opt a participant into allocation, and being allotted does not
    require having paid.
    """
    from routers.hostels import HOSTEL_FEE
    from routers.mess import MESS_FEE

    mess_paid = hostel_paid = 0
    methods = ("upi", "card", "netbanking")

    for student in students:
        if student.stay in ("both", "mess") and rng.random() < MESS_PAID_SHARE:
            student.mess_payment = simulate_payment(
                "mess", MESS_FEE, rng.choice(methods)
            )
            student.mess_payment["paid_at"] = timestamp_between(
                rng, student.created_at, REGISTRATION_CLOSES
            )
            mess_paid += 1
        if student.stay in ("both", "accommodation") and rng.random() < HOSTEL_PAID_SHARE:
            student.accommodation_payment = simulate_payment(
                "hostel", HOSTEL_FEE, rng.choice(methods)
            )
            student.accommodation_payment["paid_at"] = timestamp_between(
                rng, student.created_at, REGISTRATION_CLOSES
            )
            hostel_paid += 1

    return mess_paid, hostel_paid


# =============================================================================
# Event and workshop registrations
# =============================================================================


def assign_event_registrations(
    rng: random.Random, students: list[Student], events: list[dict]
) -> None:
    """
    Register students for events the way a fest actually fills up.

    Three independent sources of variation, which together stop the result
    looking generated: each event draws a lognormal popularity for the run, so
    some are three times as busy as others; each student draws an activity tier,
    so a third register for nothing and a few for a dozen; and each
    student-event pair gets a fresh random multiplier on top of the degree lean,
    so no two students of the same degree pick the same list.

    Only the participant side is written, because that is now the only side there
    is. The event document used to carry a ``logs`` roster mirroring every
    registration; the restructure removed it, and ``GET /events/{id}/capacity``
    and ``GET /events/{id}/participation`` both count ``participants.events``
    directly. Writing to it now would create a field no reader consults.

    Attendance scans *are* written, to ``event_logs`` — the collection
    ``POST /events/{id}/scan`` writes and ``_unique_attendance_today`` reduces —
    attributed to a real member of that event's own ``event_team``.
    """
    if not events:
        return

    popularity = [rng.lognormvariate(0, 0.62) for _ in events]
    themes = [affinity_themes(f"{e.get('name', '')} {e.get('description', '')}") for e in events]
    # Team events can carry a team; solo events never do.
    is_team = [int((e.get("team") or {}).get("max", 1) or 1) > 1 for e in events]
    # Who could plausibly have scanned for each event: its own team. An empty list
    # means `seed_staff.py --assign` has not run, in which case no scan is
    # recorded rather than one being attributed to nobody.
    scanners = [
        [m.get("user_id") for m in (e.get("event_team") or []) if m.get("user_id")]
        for e in events
    ]
    team_sequence = TEAM_ID_SEQUENCE_BASE

    for student in students:
        low, high = pick_pair(rng, tuple((band[:2], band[2]) for band in EVENT_ACTIVITY_TIERS))
        wanted = rng.randint(low, high)
        if wanted <= 0:
            continue

        theme = DEGREE_THEME[student.degree.code]
        weights = []
        for base, item_themes in zip(popularity, themes):
            lean = 1.0
            if theme in item_themes:
                lean += AFFINITY_BOOST
            elif item_themes & {"data", "electronics", "management", "aerospace"}:
                lean += AFFINITY_OFF_THEME_BOOST
            weights.append(base * lean * rng.uniform(0.45, 1.75))

        # A student cannot register before their account exists or before the
        # window opens, whichever is later.
        earliest = max(REGISTRATION_OPENS, student.created_at + timedelta(hours=1))
        if earliest >= REGISTRATION_CLOSES:
            continue

        for index in weighted_sample(rng, list(range(len(events))), weights)[:wanted]:
            event = events[index]
            team_id = None
            if is_team[index] and rng.random() < 0.45:
                # A backend-assigned id, not a display name. `team_name` is what a
                # client sends; the id it gets back is what is stored, and it is
                # what a teammate later sends to join.
                team_id = mint_team_id(event.get("event_type", "others"), team_sequence)
                team_sequence += 1
            student.events.append({
                "team_id": team_id,
                "event_id": event["_id"],
                # Matches the backend: creating a team makes you its leader, and
                # everybody else — solo registrants included — is a "member".
                "team_role": "leader" if team_id else "member",
                "registration_data": {},
            })
            registered_at = timestamp_between(rng, earliest, REGISTRATION_CLOSES)
            student.event_logs.append((event["_id"], registered_at))

            # An attendance scan, by somebody who could actually have taken it.
            if scanners[index] and rng.random() < EVENT_SCANNED_SHARE:
                student.event_scans.append(
                    (
                        event["_id"],
                        rng.choice(scanners[index]),
                        timestamp_between(rng, registered_at, REGISTRATION_CLOSES),
                    )
                )


def assign_workshop_registrations(
    rng: random.Random, students: list[Student], workshops: list[dict]
) -> dict[Any, int]:
    """
    Register students for workshops, respecting slots and seats.

    Two hard constraints the backend enforces and this has to match, or the
    seeded rows would describe a state the API would have refused: one workshop
    per ``slot_id`` per student, and never more registrations than ``capacity``.
    Both are checked as picks are walked down, which is why the weighted sample
    returns a full ordering rather than a fixed number of winners.

    Returns the per-workshop increment for ``registration_count``.
    """
    if not workshops:
        return {}

    popularity = [rng.lognormvariate(0, 0.68) for _ in workshops]
    themes = [
        affinity_themes(f"{w.get('name', '')} {w.get('description', '')}") for w in workshops
    ]
    seats_taken = {w["_id"]: int(w.get("registration_count", 0) or 0) for w in workshops}
    capacity = {w["_id"]: int(w.get("capacity", 0) or 0) for w in workshops}
    added: Counter = Counter()

    for student in students:
        low, high = pick_pair(
            rng, tuple((band[:2], band[2]) for band in WORKSHOP_ACTIVITY_TIERS)
        )
        wanted = rng.randint(low, high)
        if wanted <= 0:
            continue

        theme = DEGREE_THEME[student.degree.code]
        weights = []
        for base, item_themes in zip(popularity, themes):
            lean = 1.0
            if theme in item_themes:
                lean += AFFINITY_BOOST
            elif item_themes & {"data", "electronics", "management", "aerospace"}:
                lean += AFFINITY_OFF_THEME_BOOST
            weights.append(base * lean * rng.uniform(0.45, 1.8))

        earliest = max(REGISTRATION_OPENS, student.created_at + timedelta(hours=1))
        if earliest >= REGISTRATION_CLOSES:
            continue

        booked_slots: set[str] = set()
        for index in weighted_sample(rng, list(range(len(workshops))), weights):
            if len(student.workshops) >= wanted:
                break
            workshop = workshops[index]
            slot = workshop.get("slot_id")
            if slot in booked_slots:
                continue
            if seats_taken[workshop["_id"]] >= capacity[workshop["_id"]]:
                continue
            booked_slots.add(slot)
            seats_taken[workshop["_id"]] += 1
            added[workshop["_id"]] += 1

            # A seat taken at the door rather than booked ahead. Both charge a
            # seat against `registration_count`; what differs is `booking_type`,
            # and an on-spot attendee is present by definition.
            on_spot = rng.random() < WORKSHOP_ON_SPOT_SHARE
            attended = True if on_spot else rng.random() < WORKSHOP_ATTENDED_SHARE

            student.workshops.append({
                "slot_id": slot,
                "booking_type": "on-spot" if on_spot else "pre-registered",
                "workshop_id": workshop["_id"],
                "attended": attended,
            })
            registered_at = timestamp_between(rng, earliest, REGISTRATION_CLOSES)
            student.workshop_logs.append((workshop["_id"], registered_at))
            if attended:
                # `participant_count` follows this, and a log row records the scan
                # the same way `workshop_attendance` does.
                student.workshop_attendance.append(
                    (workshop["_id"], timestamp_between(rng, registered_at, REGISTRATION_CLOSES))
                )

    return dict(added)


# =============================================================================
# Documents
# =============================================================================


def to_document(
    student: Student,
    password_hash: str,
    key_pair: tuple[str, str],
    embedding: list[float],
    batch: str,
) -> dict[str, Any]:
    """
    The participant document, in the shape the API produces.

    ``profile`` holds the fourteen keys ``PATCH /profile/complete`` writes plus
    the academic record this dataset adds. ``program`` and ``course_stage`` are
    derived from ``degree`` and ``academic_level`` rather than stored twice, so
    the canonical pair the app reads can never disagree with the richer fields
    beside them.
    """
    private_key, public_key = key_pair
    takes_mess = student.stay in ("both", "mess")
    takes_room = student.stay in ("both", "accommodation")

    return {
        "participant_id": student.participant_id,
        "email": student.email,
        "password_hash": password_hash,
        "profile": {
            # --- exactly what the profile form collects -----------------------
            "full_name": student.full_name,
            "dob": student.dob,
            "house": student.house,
            "gender": student.gender,
            "phone": student.phone,
            "mess_preference": student.mess_preference,
            "country": student.country,
            "state": student.state,
            "city": student.city,
            "address": student.address,
            "emergency_contact": student.emergency_contact,
            "program": student.degree.code,
            "course_stage": LEVEL_TO_STAGE[student.level_name],
            "event_preferences": student.event_preferences,
            # --- the academic record this dataset adds -----------------------
            "degree": student.degree.name,
            "degree_code": student.degree.code,
            "degree_type": student.degree_type,
            "degree_started": student.degree.start_label,
            "roll_number": student.roll_number,
            "entry_year": student.entry_year,
            # The 1/2/3 in the roll number. Entry term, not academic level.
            "entry_term": student.entry_term,
            "academic_level": student.level_name,
            "academic_level_number": student.level_number,
            "nationality": student.nationality,
            "age": student.age,
        },
        # Both sub-documents are written key-for-key as `POST /auth/register`
        # writes them (and as `testing/factories.participant_doc` mirrors), because
        # every read path projects specific keys out of them. The old shapes —
        # `mess.entries` and `accommodation.logged_in` — do not exist in the
        # schema: `entries` was replaced by `scans`, keyed by the hall's own menu,
        # and `logged_in` by `inside`.
        "mess": {
            "registered": takes_mess,
            "mess_id": student.mess_oid,
            "scans": student.mess_scans,
            "payment": student.mess_payment,
        },
        "accommodation": {
            "registered": takes_room,
            # Explicitly None, never absent: `POST /hostels/allocate` selects on
            # `hostel_id: None`, and a missing key would not match that filter.
            "hostel_id": student.hostel_id,
            "room": student.room,
            "arrival": student.arrival,
            "inside": student.inside,
            "departure": student.departure,
            "payment": student.accommodation_payment,
        },
        "photo": student.photo,
        "qr_secrets": {"private_key": private_key, "public_key": public_key},
        "embedding": {"workshop": embedding, "event": embedding},
        "events": student.events,
        "workshops": student.workshops,
        "created_at": student.created_at,
        # Profile completion lands a little after sign-up.
        "updated_at": student.created_at + timedelta(minutes=7 + (student.age % 90)),
        # The one field the API never writes. It is what `--wipe` targets, so a
        # real participant can never be deleted by this script.
        "seed_source": SEED_MARKER,
        "seed_batch": batch,
    }


# =============================================================================
# Database access
# =============================================================================


def read_existing() -> dict[str, set[str]]:
    """Roll numbers, emails and phones already stored, so this run never clashes."""
    rolls: set[str] = set()
    emails: set[str] = set()
    phones: set[str] = set()
    with SessionLocal() as session:
        for person in session.scalars(select(Participant)).all():
            emails.add(person.email_normalized)
            profile = person.profile or {}
            if profile.get("roll_number"):
                rolls.add(profile["roll_number"])
            if person.phone:
                phones.add(person.phone)
    return {"rolls": rolls, "emails": emails, "phones": phones}


def load_catalogues() -> dict[str, list[dict]]:
    """The relational event, workshop, hostel and mess rows used by this seed."""
    with SessionLocal() as session:
        events = []
        for row in session.scalars(select(Event)).all():
            attrs = row.attributes or {}
            registrations = session.scalars(select(EventRegistration).where(
                EventRegistration.event_id == row.id)).all()
            team_rows = session.execute(select(EventStaff, StaffUser).join(StaffUser,
                StaffUser.id == EventStaff.staff_id).where(EventStaff.event_id == row.id)).all()
            events.append({"_id": row.id, "event_id": row.event_id, "event_type": row.category,
                "name": row.name, "description": attrs.get("description", ""),
                "team": attrs.get("team") or {},
                "event_team": [{"user_id": staff.paradox_id, "role": membership.role}
                               for membership, staff in team_rows],
                "registration_count": len(registrations)})
        workshops = []
        for row in session.scalars(select(Workshop)).all():
            registrations = session.scalar(select(func.count()).select_from(WorkshopRegistration).where(
                WorkshopRegistration.workshop_id == row.id, WorkshopRegistration.status == "registered")) or 0
            attendance = session.scalar(select(func.count()).select_from(WorkshopAttendance).where(
                WorkshopAttendance.workshop_id == row.id)) or 0
            workshops.append({"_id": row.id, "workshop_id": row.workshop_id,
                "slot_id": session.get(WorkshopSlot, row.slot_id).slot_id, "name": row.name,
                "description": (row.attributes or {}).get("description", ""), "capacity": row.capacity,
                "registration_count": registrations, "participant_count": attendance})
        hostels = []
        for row in session.scalars(select(Hostel)).all():
            rooms = session.scalars(select(HostelRoom).where(HostelRoom.hostel_id == row.id).order_by(HostelRoom.room_number)).all()
            room_rows = []
            for room in rooms:
                occupants = session.scalars(select(Participant.participant_id).join(
                    AccommodationAllocation, AccommodationAllocation.participant_id == Participant.id).where(
                    AccommodationAllocation.room_id == room.id)).all()
                room_rows.append({"room_number": room.room_number, "capacity": room.capacity,
                                  "occupants": list(occupants)})
            hostels.append({"_id": row.id, "hostel_id": row.hostel_id, "gender": row.gender,
                "capacity": row.capacity, "name": row.name, "sharing": row.sharing,
                "rooms": room_rows,
                "current_occupancy": row.current_occupancy})
        mess = []
        for row in session.scalars(select(Mess)).all():
            occupied = session.scalar(select(func.count()).select_from(MessAllocation).where(
                MessAllocation.mess_id == row.id, MessAllocation.status == "allocated")) or 0
            mess.append({"_id": row.id, "mess_id": row.mess_id, "type": row.type,
                         "capacity": row.capacity, "name": row.name, "menu": _menu_for_seed(row.menu or {}),
                         "current_occupancy": occupied})
        return {"events": events, "workshops": workshops, "hostels": hostels, "mess": mess}


def _menu_for_seed(menu: dict) -> dict:
    """Convert PostgreSQL's ISO menu timestamps back to datetimes for sampling."""
    result = {}
    for day, slots in (menu or {}).items():
        result[day] = {}
        for name, data in (slots or {}).items():
            sitting = dict(data or {})
            for key in ("start_time", "end_time"):
                value = sitting.get(key)
                if isinstance(value, str):
                    try:
                        sitting[key] = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError:
                        pass
            result[day][name] = sitting
    return result


def _json_safe(value):
    """Make values accepted by JSONB while preserving ISO-8601 instants."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


DATA_DIR = Path(__file__).resolve().parent.parent / "frontend" / "src" / "data"


_DATASET_FILES = {
    "events": "paradoxEvents.json",
    "workshops": "paradoxWorkshops.json",
    "hostels": "paradoxHostels.json",
    "mess": "paradoxMess.json",
}


def _dataset(kind: str) -> list[dict]:
    return json.loads((DATA_DIR / _DATASET_FILES[kind]).read_text("utf-8"))


# =============================================================================
# Demo catalogue
# =============================================================================
#
# Every builder below produces the document shape the corresponding route
# produces today — cross-checked field by field against `testing/factories.py`,
# which mirrors each route and cites it. They exist because the frontend datasets
# are still written for the pre-restructure schema, and a demo catalogue in the
# old shape is worse than none: allocation silently places nobody, and the seeded
# cohort validates against documents the API would never have written.
#
# The old versions of these wrote `category`/`occupancy`/`coordinator` on hostels,
# `preference`/`cuisines` on halls, `open`/`logs` on events, and workshops with no
# slot, no start time and no registration window. None of those fields exist any
# more, and the ones that replaced them were all missing.


def _demo_hostel_docs(now: datetime) -> list[dict]:
    """Hostels in the shape ``POST /hostels`` writes."""
    records = sorted(_dataset("hostels"), key=lambda r: r["name"])
    docs = []
    for index, record in enumerate(records):
        rooming = seed_calendar.hostel_rooming(record["name"], record["capacity"], index)
        docs.append({
            "hostel_id": record["hostel_id"],
            "name": record["name"],
            "capacity": record["capacity"],
            "gender": record["gender"],
            "sharing": rooming["sharing"],
            "current_occupancy": 0,
            "rooms": [
                {"room_number": str(101 + i), "occupants": []}
                for i in range(rooming["num_rooms"])
            ],
            "hostel_team": [],
            "created_at": now,
        })
    return docs


def _demo_mess_docs(now: datetime) -> list[dict]:
    """
    Halls in the shape ``POST /mess`` writes, menu included.

    The menu uses ``seed_calendar.mess_menu_datetimes`` — real ``datetime``
    objects, not ISO strings. ``mess._assert_mess_scan_window`` silently disables
    itself for a sitting whose bounds are not datetimes, so strings here would
    leave the hall scannable at any hour of any day.
    """
    menu = seed_calendar.mess_menu_datetimes()
    return [
        {
            "mess_id": record["mess_id"],
            "name": record["name"],
            "capacity": record["capacity"],
            "type": seed_calendar.mess_type(
                record["mess_id"], record.get("preference"), record.get("cuisines")
            ),
            "menu": menu,
            "mess_team": [],
            "created_at": now,
        }
        for record in sorted(_dataset("mess"), key=lambda r: r["mess_id"])
    ]


def _demo_slot_docs(now: datetime) -> list[dict]:
    """
    The workshop slots, in the shape ``POST /workshop-slots`` writes.

    Absent from the old demo catalogue entirely, which is why a workshop seeded
    by it had no time at all: ``start_time`` is denormalized from the slot, and
    with no slot there was nothing to denormalize.
    """
    return [
        {
            "slot_id": slot_id,
            "start_time": bounds["start_time"],
            "end_time": bounds["end_time"],
            "created_by": None,
            "created_at": now,
            "updated_at": now,
        }
        for slot_id, bounds in sorted(seed_calendar.SLOT_TIMES.items())
    ]


def _demo_workshop_docs(now: datetime) -> list[dict]:
    """Workshops in the shape ``POST /workshops`` writes."""
    window = seed_calendar.workshop_registration_window()
    docs = []
    for record in sorted(_dataset("workshops"), key=lambda r: r["name"]):
        slot_id = seed_calendar.legacy_slot_id(record["slot_id"])
        docs.append({
            "workshop_id": record["workshop_id"],
            "slot_id": slot_id,
            "name": record["name"],
            "description": record["description"],
            "embedding": zero_embedding(),
            "venue": record["venue"],
            "capacity": record["capacity"],
            "registration_count": 0,
            "participant_count": 0,
            "instructions": record.get("instructions", ""),
            # Denormalized from the slot, exactly as `create_workshop` does.
            "start_time": seed_calendar.SLOT_TIMES[slot_id]["start_time"],
            **window,
            "registration_closed_by_system": False,
            "workshop_team": [],
            "created_by": None,
            "created_at": now,
            "updated_at": now,
        })
    return docs


def _demo_event_docs(now: datetime) -> list[dict]:
    """Events in the shape ``POST /events`` writes."""
    docs = []
    for record in sorted(_dataset("events"), key=lambda r: r["name"]):
        rounds = record.get("schedule") or []
        times = seed_calendar.event_round_times(record["name"], len(rounds))
        event_type = record["event_type"]
        team = record.get("team") or {}
        docs.append({
            "event_id": record["event_id"],
            "event_type": event_type,
            "name": record["name"],
            "description": record["description"],
            "embedding": zero_embedding(),
            "poster": record.get("poster", ""),
            "team": {
                "min": team.get("min", 1),
                "max": team.get("max", 1),
                # Renamed; the dataset still calls it `house`.
                "house_vs_house_event": bool(team.get("house", False)),
                "allow_single_registration": team.get("allow_single_registration", True),
            },
            "prize_money": record.get("prize_money", []),
            # `{start_time, end_time, allowed}`. The dataset's own `registration`
            # object is presentation copy (rulebook, faqs, meta) and has no place
            # in the schema. `is_open` is never stored — it is computed per read.
            "registration": seed_calendar.event_registration_window(),
            "schedule": [
                {
                    "round_id": f"RND{event_type[:3].upper()}{11111 + i}",
                    "name": rnd["name"],
                    "description": rnd.get("description") or "",
                    "start_time": seed_calendar.iso(start),
                    "end_time": seed_calendar.iso(end),
                    "venue": rnd.get("venue"),
                }
                for i, (rnd, (start, end)) in enumerate(zip(rounds, times))
            ],
            "registration_fields": record.get("registration_fields", []),
            "event_team": [],
            # Present and empty, as `create_event` writes it. There is no `logs`
            # array and no top-level `open` flag any more.
            "announcements": [],
            "created_by": None,
            "created_at": now,
            "updated_at": now,
        })
    return docs


def in_memory_catalogue(kind: str) -> list[dict]:
    """
    A catalogue built from the frontend dataset, with invented ids.

    Only for ``--dry-run --demo-catalogue`` on a database whose catalogue is
    empty: the run needs events, workshops, blocks and halls to hang
    registrations off, and a dry run is not allowed to create them. The ids are
    throwaway, which is fine because nothing is written — but the *shapes* are
    the real ones, so the allocation and scan passes behave exactly as they will
    against a real catalogue.
    """
    now = datetime.now(timezone.utc)
    builders = {
        "events": _demo_event_docs,
        "workshops": _demo_workshop_docs,
        "hostels": _demo_hostel_docs,
        "mess": _demo_mess_docs,
    }
    docs = builders[kind](now)
    for doc in docs:
        doc["_id"] = uuid.uuid4()
    return docs


def seed_demo_catalogue(log=print) -> None:
    """
    Fill empty catalogue collections from the frontend datasets.

    A convenience for an empty local database and the only way to exercise this
    script against the in-memory ``TESTING=1`` client, which starts with nothing
    in it. The real path for a deployment is ``seed_staff.py`` then ``seed.py`` /
    ``seed_mess.py`` / ``seed_events.py`` / ``seed_workshops.py``, all of which go
    through the API; this writes directly, and only ever fills a collection that
    is completely empty, so it can never overwrite what those wrote.
    """
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        if not session.scalar(select(func.count()).select_from(Hostel)):
            for doc in _demo_hostel_docs(now):
                hostel = Hostel(hostel_id=doc["hostel_id"], name=doc["name"], gender=doc["gender"],
                    capacity=doc["capacity"], sharing=doc["sharing"], current_occupancy=0,
                    attributes={"seed_source": SEED_MARKER}, created_at=now, updated_at=now)
                session.add(hostel); session.flush()
                for room in doc["rooms"]:
                    session.add(HostelRoom(hostel_id=hostel.id, room_number=room["room_number"],
                        capacity=hostel.sharing))
            log(f"  demo catalogue: {len(_demo_hostel_docs(now))} hostels")
        if not session.scalar(select(func.count()).select_from(Mess)):
            docs = _demo_mess_docs(now)
            session.add_all([Mess(mess_id=d["mess_id"], name=d["name"], type=d["type"],
                capacity=d["capacity"], menu=_json_safe(d["menu"]), attributes={"seed_source": SEED_MARKER},
                created_at=now, updated_at=now) for d in docs])
            log(f"  demo catalogue: {len(docs)} mess halls")
        if not session.scalar(select(func.count()).select_from(WorkshopSlot)):
            docs = _demo_slot_docs(now)
            session.add_all([WorkshopSlot(slot_id=d["slot_id"], start_time=d["start_time"],
                end_time=d["end_time"], attributes={"seed_source": SEED_MARKER}) for d in docs])
            log(f"  demo catalogue: {len(docs)} workshop slots")
        session.flush()
        if not session.scalar(select(func.count()).select_from(Workshop)):
            docs = _demo_workshop_docs(now)
            slots = {r.slot_id: r.id for r in session.scalars(select(WorkshopSlot)).all()}
            for d in docs:
                config = {k: d[k] for k in ("registration_start", "registration_end", "registration_open", "registration_closed_by_system")}
                session.add(Workshop(workshop_id=d["workshop_id"], name=d["name"], slot_id=slots[d["slot_id"]],
                    start_time=d["start_time"], capacity=d["capacity"], registration_config=config,
                    attributes={"description": d["description"], "embedding": d["embedding"],
                        "venue": d["venue"], "instructions": d["instructions"], "seed_source": SEED_MARKER},
                    created_at=now, updated_at=now))
            log(f"  demo catalogue: {len(docs)} workshops")
        if not session.scalar(select(func.count()).select_from(Event)):
            docs = _demo_event_docs(now)
            for d in docs:
                session.add(Event(event_id=d["event_id"], name=d["name"], category=d["event_type"],
                    registration_config=d["registration"], schedule=d["schedule"],
                    attributes={k: d.get(k) for k in ("description", "embedding", "poster", "team", "prize_money", "registration_fields", "announcements")} | {"seed_source": SEED_MARKER},
                    created_at=now, updated_at=now))
            log(f"  demo catalogue: {len(docs)} events")


def write_students(students: list[Student], documents: list[dict], log=print) -> None:
    """Persist a generated cohort into the normalized relational tables."""
    from datetime import date
    with SessionLocal.begin() as session:
        events = {str(r.id): r for r in session.scalars(select(Event)).all()}
        workshops = {str(r.id): r for r in session.scalars(select(Workshop)).all()}
        messes = {str(r.id): r for r in session.scalars(select(Mess)).all()}
        hostels = {r.hostel_id: r for r in session.scalars(select(Hostel)).all()}
        staff = {r.paradox_id: r for r in session.scalars(select(StaffUser)).all()}
        people = {}
        doc_by_id = {d["participant_id"]: d for d in documents}
        for student in students:
            doc = doc_by_id[student.participant_id]
            profile = dict(doc["profile"])
            p = Participant(participant_id=doc["participant_id"], email=doc["email"],
                email_normalized=doc["email"].strip().lower(), password_hash=doc["password_hash"],
                full_name=student.full_name, date_of_birth=date.fromisoformat(student.dob),
                house=student.house, gender=student.gender, phone=student.phone, country=student.country,
                state=student.state, city=student.city, address=student.address,
                program=student.degree.code, course_stage=profile.get("course_stage"),
                mess_preference=student.mess_preference, profile=profile,
                preferences={"event_preferences": student.event_preferences},
                qr_secrets=doc["qr_secrets"], embedding=doc["embedding"], photo=student.photo,
                mess_state={"registered": student.stay in ("both", "mess"), "scans": {},
                            "payment": ({**student.mess_payment, "paid_at": student.mess_payment["paid_at"].isoformat()}
                                        if student.mess_payment else None)},
                accommodation_state={"registered": student.stay in ("both", "accommodation"),
                    "room": student.room, "inside": student.inside,
                    "arrival": student.arrival.isoformat() if student.arrival else None,
                    "departure": student.departure.isoformat() if student.departure else None,
                    "payment": ({**student.accommodation_payment, "paid_at": student.accommodation_payment["paid_at"].isoformat()}
                                if student.accommodation_payment else None)},
                attributes={"seed_source": SEED_MARKER, "seed_batch": doc["seed_batch"]},
                created_at=student.created_at, updated_at=doc["updated_at"])
            session.add(p); people[student.participant_id] = (p, student)
        session.flush()

        registered_counts, attended_counts = Counter(), Counter()
        for p, student in people.values():
            if student.mess_oid:
                mess = messes.get(str(student.mess_oid))
                if mess:
                    session.add(MessAllocation(participant_id=p.id, mess_id=mess.id,
                        payment_status="paid" if student.mess_payment else None,
                        paid_at=(student.mess_payment or {}).get("paid_at")))
            if student.hostel_id and student.room is not None:
                hostel = hostels.get(student.hostel_id)
                room = next((r for r in hostel.rooms if r.room_number == student.room), None) if hostel else None
                if room:
                    session.add(AccommodationAllocation(participant_id=p.id, room_id=room.id,
                        arrival_at=student.arrival, departure_at=student.departure, is_inside=student.inside,
                        payment_status="paid" if student.accommodation_payment else None,
                        paid_at=(student.accommodation_payment or {}).get("paid_at")))
                    hostel.current_occupancy += 1
            for entry in student.events:
                event = events.get(str(entry["event_id"]))
                if not event:
                    continue
                team_id = entry.get("team_id")
                registered_at = next((when for event_id, when in student.event_logs
                                       if str(event_id) == str(entry["event_id"])), student.created_at)
                reg = EventRegistration(participant_id=p.id, event_id=event.id,
                    event_identifier=event.event_id, team_id=team_id, team_identifier=team_id,
                    answers=entry.get("registration_data") or {},
                    attributes={"team_role": entry.get("team_role", "member")},
                    registered_at=registered_at)
                session.add(reg)
                if team_id:
                    team = ParticipantTeam(event_id=event.id, team_id=team_id,
                        leader_id=p.id, attributes={"seed_source": SEED_MARKER})
                    session.add(team); session.flush()
                    session.add(ParticipantTeamMember(team_id=team.id, participant_id=p.id, role="leader"))
            for entry in student.workshops:
                workshop = workshops.get(str(entry["workshop_id"]))
                if not workshop:
                    continue
                registered_at = next((when for workshop_id, when in student.workshop_logs
                                      if str(workshop_id) == str(entry["workshop_id"])), student.created_at)
                session.add(WorkshopRegistration(participant_id=p.id, workshop_id=workshop.id,
                    slot_id=workshop.slot_id, attributes={"booking_type": entry["booking_type"],
                    "attended": bool(entry.get("attended")), "seed_source": SEED_MARKER},
                    registered_at=registered_at))
                registered_counts[workshop.id] += 1
            for workshop_id, when in student.workshop_attendance:
                workshop = workshops.get(str(workshop_id))
                if workshop:
                    session.add(WorkshopAttendance(workshop_id=workshop.id, participant_id=p.id,
                        scan_type="on-spot" if any(e.get("workshop_id") == workshop_id and e.get("booking_type") == "on-spot" for e in student.workshops) else "pre-registered",
                        scanned_at=when, details={"seed_source": SEED_MARKER}))
                    attended_counts[workshop.id] += 1
            for workshop_id, when in student.workshop_logs:
                workshop = workshops.get(str(workshop_id))
                if workshop:
                    session.add(WorkshopLog(workshop_id=workshop.id, participant_id=p.id,
                        action="registration", created_at=when, details={"seed_source": SEED_MARKER}))
            for workshop_id, when in student.workshop_attendance:
                workshop = workshops.get(str(workshop_id))
                if workshop:
                    session.add(WorkshopLog(workshop_id=workshop.id, participant_id=p.id,
                        action="attendance", created_at=when, details={"seed_source": SEED_MARKER}))
            for event_id, scanner_id, when in student.event_scans:
                event = events.get(str(event_id))
                if event:
                    session.add(EventAttendance(event_id=event.id, event_identifier=event.event_id,
                        participant_id=p.id, scanner_id=staff[scanner_id].id if scanner_id in staff else None,
                        scan_type="event", service_date=when.date(), scanned_at=when,
                        details={"scanner_user_id": scanner_id, "seed_source": SEED_MARKER}))
            for mess_id, meal, day, when in student.mess_scan_rows:
                mess = next((m for m in messes.values() if m.mess_id == mess_id), None)
                session.add(MessAttendance(participant_id=p.id, mess_id=mess.id if mess else None,
                    mess_identifier=mess_id, day_number=day, service_date=when.date(), meal=meal, scanned_at=when))
            for facility, payment in (("mess", student.mess_payment), ("hostel", student.accommodation_payment)):
                if payment:
                    facility_id = next((m.mess_id for m in messes.values() if m.id == student.mess_oid), "") if facility == "mess" else student.hostel_id or ""
                    session.add(Payment(participant_id=p.id, facility_type=facility, facility_id=facility_id,
                        transaction_id=payment["transaction_id"], amount=payment["amount"], status="paid",
                        method=payment.get("method"), settled_at=payment.get("paid_at"),
                        details={"seed_source": SEED_MARKER}))
            for audit in _audit_rows_for(student):
                details = dict(audit["details"])
                details["seed_source"] = SEED_MARKER
                session.add(SystemLog(actor_id=audit["actor_id"], actor_name=audit["actor_name"],
                    actor_type=audit["actor_type"], actor_role=audit["actor_role"], action=audit["action"],
                    target_id=audit.get("target_id"), details=details, created_at=audit["timestamp"]))
        log(f"  participants inserted: {len(documents)}")
        log(f"  workshop registrations: {sum(registered_counts.values())}; attendance: {sum(attended_counts.values())}")


def _audit_rows_for(student: Student) -> list[dict]:
    """
    The audit rows a student's own actions would have left behind.

    Shaped exactly as ``logger.log_audit`` writes them (and as
    ``testing/factories.audit_row`` mirrors), because ``GET /audit-logs`` and
    ``GET /audit-logs/summary`` read this collection directly. The ``MESS_SCAN``
    rows are the load-bearing ones: the dashboard's "meals served" figure is
    reduced from them, de-duplicated per diner, and populating ``mess.scans``
    alone leaves that figure at zero.

    ``seed_source`` is carried on every row so ``--wipe`` can remove exactly
    these and nothing else.
    """
    rows: list[dict] = []

    def row(action: str, target_id: Any, when: datetime, details: dict) -> dict:
        return {
            "timestamp": when,
            "actor_id": student.participant_id,
            "actor_name": student.full_name,
            "actor_type": "participant",
            "actor_role": "participant",
            "action": action,
            "target_id": target_id,
            "details": details,
            "seed_source": SEED_MARKER,
        }

    rows.append(
        row("REGISTER", student.participant_id, student.created_at,
            {"email_local": student.email.split("@")[0], "program": student.degree.code})
    )
    for mess_id, slot, day, when in student.mess_scan_rows:
        rows.append(
            row("MESS_SCAN", mess_id, when,
                {"participant_id": student.participant_id, "slot": slot, "day": day})
        )
    if student.mess_payment:
        rows.append(
            row("MESS_PAYMENT", student.participant_id, student.mess_payment["paid_at"],
                {
                    "transaction_id": student.mess_payment["transaction_id"],
                    "amount": student.mess_payment["amount"],
                    "method": student.mess_payment.get("method"),
                })
        )
    if student.accommodation_payment:
        rows.append(
            row("HOSTEL_PAYMENT", student.participant_id,
                student.accommodation_payment["paid_at"],
                {
                    "transaction_id": student.accommodation_payment["transaction_id"],
                    "amount": student.accommodation_payment["amount"],
                    "method": student.accommodation_payment.get("method"),
                })
        )
    return rows


def wipe_seeded(log=print) -> dict[str, int]:
    """Delete seeded participants and their relational child records atomically."""
    empty = {"participants": 0, "workshop_logs": 0, "event_logs": 0,
             "audit_rows": 0, "occupants": 0}
    with SessionLocal.begin() as session:
        people = session.scalars(select(Participant).where(
            Participant.attributes["seed_source"].as_string() == SEED_MARKER)).all()
        if not people:
            log("  nothing to remove")
            return empty
        ids = [p.id for p in people]
        actor_ids = [p.participant_id for p in people]
        occupant_rows = session.execute(select(AccommodationAllocation, HostelRoom).join(
            HostelRoom, HostelRoom.id == AccommodationAllocation.room_id).where(
            AccommodationAllocation.participant_id.in_(ids))).all()
        occupancy = Counter()
        for allocation, room in occupant_rows:
            occupancy[room.hostel_id] += 1
        for hostel_id, count in occupancy.items():
            hostel = session.get(Hostel, hostel_id)
            if hostel:
                hostel.current_occupancy = max(0, hostel.current_occupancy - count)
        workshop_logs = session.scalar(select(func.count()).select_from(WorkshopLog).where(
            WorkshopLog.participant_id.in_(ids))) or 0
        event_logs = session.scalar(select(func.count()).select_from(EventAttendance).where(
            EventAttendance.participant_id.in_(ids))) or 0
        audit_rows = session.scalar(select(func.count()).select_from(SystemLog).where(
            SystemLog.actor_id.in_(actor_ids), SystemLog.details["seed_source"].as_string() == SEED_MARKER)) or 0
        session.execute(delete(WorkshopLog).where(WorkshopLog.participant_id.in_(ids)))
        session.execute(delete(EventAttendance).where(EventAttendance.participant_id.in_(ids)))
        session.execute(delete(MessAttendance).where(MessAttendance.participant_id.in_(ids)))
        session.execute(delete(WorkshopAttendance).where(WorkshopAttendance.participant_id.in_(ids)))
        session.execute(delete(Payment).where(Payment.participant_id.in_(ids)))
        session.execute(delete(ParticipantTeam).where(
            ParticipantTeam.attributes["seed_source"].as_string() == SEED_MARKER))
        session.execute(delete(SystemLog).where(SystemLog.actor_id.in_(actor_ids),
            SystemLog.details["seed_source"].as_string() == SEED_MARKER))
        removed = len(people)
        for person in people:
            session.delete(person)
    result = {"participants": removed, "workshop_logs": workshop_logs,
              "event_logs": event_logs, "audit_rows": audit_rows,
              "occupants": sum(occupancy.values())}
    log(f"  removed {removed} seeded participants, {workshop_logs} workshop log rows, "
        f"{event_logs} event attendance rows, {audit_rows} audit rows, {result['occupants']} allocations")
    return result


#: Migration map for a data-quality issue this script itself caused before it
#: started importing `HOUSES` from `models` — see the note on that import,
#: above. An older version of this file carried a *local* house-name table with
#: a `" House"` suffix ("Bandipur House"), so every participant it seeded has
#: that suffixed form sitting in `profile.house` in the live database, even
#: though `models.HOUSES`/`ProfileCompleteRequest` have used the bare form for
#: a long time. Built from `models.HOUSES` rather than hand-typed, so it can
#: never map two houses onto the same key or fall out of step with the current
#: vocabulary.
HOUSE_NAME_FIXUPS: dict[str, str] = {f"{house} House": house for house in HOUSES}


def fix_house_names(log=print) -> dict[str, int]:
    """
    Rewrite every already-seeded participant's suffixed house name to the bare
    form ``models.HOUSES`` expects, in place.

    Scoped to the twelve known suffixed forms (``HOUSE_NAME_FIXUPS``) rather than
    a blanket ``profile.house`` rewrite, so a house value this migration does not
    recognise is left untouched and reported instead of silently coerced into
    something wrong. Idempotent: a participant already on the bare form has
    nothing matching any of these keys, so a second run touches zero documents.

    Deliberately not scoped to ``seed_source`` — the fix is for the value
    itself, wherever it is found, not for who wrote it. A bare-name-only backend
    validator (``ProfileCompleteRequest``) means no *real* participant could
    ever have written the suffixed form through the API in the first place, so
    this can only ever touch what an earlier run of this script wrote.
    """
    tally: dict[str, int] = {house: 0 for house in HOUSES}
    unmapped: Counter = Counter()

    for suffixed, bare in HOUSE_NAME_FIXUPS.items():
        with SessionLocal.begin() as session:
            rows = session.scalars(select(Participant).where(
                Participant.profile["house"].as_string() == suffixed)).all()
            for participant in rows:
                participant.profile = {**(participant.profile or {}), "house": bare}
            tally[bare] += len(rows)

    # Anything still not one of the twelve bare names, after the known fixups,
    # is a genuinely unrecognised value worth surfacing rather than papering
    # over — a typo or a house retired since this dataset was written, say.
    with SessionLocal() as session:
        for participant in session.scalars(select(Participant)).all():
            house = (participant.profile or {}).get("house")
            if house is not None and house not in HOUSES:
                unmapped[house] += 1

    fixed = sum(tally.values())
    log(f"  fixed {fixed} participant(s) across {sum(1 for v in tally.values() if v)} house(s)")
    for house in HOUSES:
        if tally[house]:
            log(f"    {house:<12} +{tally[house]}")
    if unmapped:
        log(f"  {sum(unmapped.values())} participant(s) still have an unrecognised house value:")
        for value, count in unmapped.most_common():
            log(f"    {value!r}  x{count}")

    return {"fixed": fixed, "unmapped": sum(unmapped.values())}


# =============================================================================
# Validation and reporting
# =============================================================================


def build_report(
    documents: list[dict],
    events: list[dict],
    workshops: list[dict],
    hostels: list[dict],
    mess_halls: list[dict],
    registration_times: Sequence[datetime],
    *,
    written: bool,
) -> dict[str, Any]:
    """
    Count what was actually generated and check every rule that has to hold.

    Deliberately reads the finished documents rather than the generator's own
    variables: on a real run these come back from PostgreSQL, so the report
    describes what landed in the database, not what the script intended.

    ``registration_times`` is every event and workshop registration timestamp, so
    the registration-window rule is checked against the timestamps themselves.
    Reading them back off the catalogue mirrors instead would mean a dry run
    examined nothing and still reported a pass.

    ``events`` and ``workshops`` are the snapshots taken *before* writing, which
    is what makes the capacity arithmetic work either way: prior count plus what
    this run added has to fit. When ``written`` is set, the mirrors are also
    checked against the participant side, so a dual write that only half landed
    is caught.
    """
    events_by_oid = {e["_id"]: e for e in events}
    workshops_by_oid = {w["_id"]: w for w in workshops}
    hostels_by_id = {h["hostel_id"]: h for h in hostels}
    halls_by_oid = {h["_id"]: h for h in mess_halls}
    # Occupancy is recounted from the students themselves, so a block cannot be
    # reported as within capacity on the strength of a counter the seed set.
    block_occupancy: Counter = Counter()
    block_rooms: dict[str, Counter] = defaultdict(Counter)
    hall_occupancy: Counter = Counter()

    counts: dict[str, Counter] = {
        key: Counter() for key in (
            "gender", "degree", "entry_year", "entry_term", "level", "degree_type",
            "nationality", "country", "age_band", "house", "stay", "course_stage",
            "mess_preference", "photo_kind", "event_popularity", "workshop_popularity",
            "events_per_student", "workshops_per_student", "region",
        )
    }
    rolls: Counter = Counter()
    emails: Counter = Counter()
    phones: Counter = Counter()
    photos: Counter = Counter()

    total = len(documents)
    international = 0
    profile_complete = 0
    hostel_registered = hostel_allotted = 0
    mess_registered = mess_allotted = 0
    event_registrations = workshop_registrations = 0
    workshop_seats_added: Counter = Counter()
    workshop_attendance_added: Counter = Counter()
    workshops_attended = workshops_on_spot = 0
    mess_scans_recorded = 0
    residents_inside = residents_departed = 0
    mess_payments = hostel_payments = 0
    problems: list[str] = []

    for doc in documents:
        profile = doc.get("profile") or {}
        roll = profile.get("roll_number", "")
        rolls[roll] += 1
        emails[doc.get("email", "")] += 1
        phones[profile.get("phone", "")] += 1
        photos[doc.get("photo", "")] += 1
        if profile.get("full_name"):
            profile_complete += 1

        code = profile.get("degree_code")
        degree = DEGREES.get(code)
        year = profile.get("entry_year")
        term = profile.get("entry_term")
        level = profile.get("academic_level")

        counts["gender"][profile.get("gender")] += 1
        counts["degree"][profile.get("degree")] += 1
        counts["entry_year"][year] += 1
        counts["entry_term"][term] += 1
        counts["level"][level] += 1
        counts["course_stage"][profile.get("course_stage")] += 1
        counts["degree_type"][profile.get("degree_type")] += 1
        counts["house"][profile.get("house")] += 1
        counts["photo_kind"]["portrait" if "randomuser" in str(doc.get("photo")) else "avatar"] += 1
        counts["mess_preference"][profile.get("mess_preference")] += 1

        nationality = profile.get("nationality")
        counts["nationality"][nationality] += 1
        if nationality != "Indian" or profile.get("country") != "India":
            counts["country"][profile.get("country")] += 1
        if profile.get("country") != "India":
            international += 1

        age = profile.get("age") or 0
        for low, high, _weight in AGE_BANDS:
            if low <= age <= high:
                counts["age_band"][f"{low}–{high}"] += 1
                break

        # --- rules -------------------------------------------------------------
        if not ROLL_PATTERN.match(str(roll)):
            problems.append(f"{roll}: roll number does not match YYF[1-3]XXXXXX")
        elif int(str(roll)[:2]) != (year or 0) % 100:
            problems.append(f"{roll}: first two digits do not match entry year {year}")
        elif int(str(roll)[3]) != term:
            problems.append(f"{roll}: term digit does not match entry term {term}")

        if not EARLIEST_ENTRY_YEAR <= (year or 0) <= LATEST_ENTRY_YEAR:
            problems.append(
                f"{roll}: entry year {year} is outside "
                f"{EARLIEST_ENTRY_YEAR}–{LATEST_ENTRY_YEAR}"
            )
        if year == 2026 and term == 3:
            problems.append(f"{roll}: 2026 term 3 does not exist yet")

        if degree is None:
            problems.append(f"{roll}: unknown degree code {code!r}")
        else:
            if not str(doc.get("email", "")).endswith("@" + degree.email_domain):
                problems.append(f"{roll}: email domain does not match {degree.name}")
            if str(roll).lower() != str(doc.get("email", "")).split("@")[0]:
                problems.append(f"{roll}: email local part is not the roll number")
            if doc.get("participant_id") != participant_id_for(doc.get("email", "")):
                problems.append(f"{roll}: participant_id is not derived from the email")
            if level not in degree.levels.values():
                problems.append(f"{roll}: {level!r} is not a level of {degree.name}")
            elif {v: k for k, v in degree.levels.items()}[level] not in degree.seed_levels:
                problems.append(f"{roll}: {level!r} is not seedable for {degree.name}")
            if (year or 0) < degree.start_year:
                problems.append(
                    f"{roll}: entered {year} but {degree.name} started {degree.start_year}"
                )
            if LEVEL_TO_STAGE.get(level) != profile.get("course_stage"):
                problems.append(f"{roll}: course_stage disagrees with academic level")
            if profile.get("program") != degree.code:
                problems.append(f"{roll}: program disagrees with degree")

        floor = max(19, MIN_ENTRY_AGE + (FEST_YEAR - (year or FEST_YEAR)))
        if age < floor:
            problems.append(f"{roll}: age {age} is impossible for a {year} entrant")

        # --- accommodation and mess -------------------------------------------
        accommodation = doc.get("accommodation") or {}
        mess = doc.get("mess") or {}
        takes_room = bool(accommodation.get("registered"))
        takes_mess = bool(mess.get("registered"))
        stay = (
            "both" if takes_room and takes_mess
            else "accommodation" if takes_room
            else "mess" if takes_mess
            else "neither"
        )
        counts["stay"][stay] += 1
        if takes_room:
            hostel_registered += 1
        if accommodation.get("payment"):
            hostel_payments += 1
        if mess.get("payment"):
            mess_payments += 1
        for day_slots in (mess.get("scans") or {}).values():
            mess_scans_recorded += sum(
                1 for marker in (day_slots or {}).values() if (marker or {}).get("scanned")
            )
        if accommodation.get("inside"):
            residents_inside += 1
        if accommodation.get("departure"):
            residents_departed += 1

        # `scan_hostel`'s own invariants, which a seeded snapshot has to satisfy or
        # it describes a state no scan sequence could have produced.
        if accommodation.get("inside") and accommodation.get("departure"):
            problems.append(f"{roll}: inside the block but marked permanently departed")
        if accommodation.get("inside") and not accommodation.get("arrival"):
            problems.append(f"{roll}: inside the block with no arrival stamped")
        if accommodation.get("departure") and not accommodation.get("arrival"):
            problems.append(f"{roll}: departed without ever arriving")
        if accommodation.get("room") and not accommodation.get("hostel_id"):
            problems.append(f"{roll}: has a room number but no block")
        if (mess.get("scans") or {}) and not mess.get("mess_id"):
            problems.append(f"{roll}: has meal scans without an allotted hall")

        if accommodation.get("hostel_id"):
            hostel_allotted += 1
            if not takes_room:
                problems.append(f"{roll}: allotted a hostel without requesting one")
            block = hostels_by_id.get(accommodation["hostel_id"])
            if block is None:
                problems.append(f"{roll}: allotted to unknown block {accommodation['hostel_id']}")
            else:
                block_occupancy[block["hostel_id"]] += 1
                block_rooms[block["hostel_id"]][accommodation.get("room")] += 1
                # `POST /hostels/allocate` buckets blocks by gender and looks the
                # student's own up, so a mismatch here is a placement the batch
                # could never have made.
                if str(block.get("gender", "")).lower() != profile.get("gender"):
                    problems.append(
                        f"{roll}: {profile.get('gender')} student in a "
                        f"{block.get('gender')} block"
                    )
        if takes_mess:
            mess_registered += 1
            if not profile.get("mess_preference"):
                problems.append(f"{roll}: takes mess but has no meal preference")
        if mess.get("mess_id"):
            mess_allotted += 1
            if not takes_mess:
                problems.append(f"{roll}: allotted a mess hall without requesting one")
            hall = halls_by_oid.get(mess["mess_id"])
            if hall is None:
                problems.append(f"{roll}: allotted to an unknown mess hall")
            else:
                hall_occupancy[hall["_id"]] += 1
                # Matched on the dietary axis alone, because that is all
                # `POST /mess/allocate` matches on: a `south_indian__veg` student
                # in a `north_indian__veg` hall is a placement the batch would
                # happily make, since the hall serves what they eat.
                if diet_of(str(hall.get("type", ""))) != diet_of(profile.get("mess_preference") or ""):
                    problems.append(
                        f"{roll}: eats {profile.get('mess_preference')} but was placed in a "
                        f"{hall.get('type')} hall"
                    )
        if not takes_mess and profile.get("mess_preference"):
            problems.append(f"{roll}: has a meal preference without taking mess")

        # --- registrations -----------------------------------------------------
        student_events = doc.get("events") or []
        student_workshops = doc.get("workshops") or []
        event_registrations += len(student_events)
        workshop_registrations += len(student_workshops)
        counts["events_per_student"][_bucket(len(student_events))] += 1
        counts["workshops_per_student"][_bucket(len(student_workshops))] += 1

        seen_events = set()
        for entry in student_events:
            oid = entry.get("event_id")
            if oid in seen_events:
                problems.append(f"{roll}: registered twice for the same event")
            seen_events.add(oid)
            event = events_by_oid.get(oid)
            if event is None:
                problems.append(f"{roll}: event registration points at no known event")
            else:
                counts["event_popularity"][event.get("name")] += 1

        seen_slots = set()
        for entry in student_workshops:
            oid = entry.get("workshop_id")
            slot = entry.get("slot_id")
            if slot in seen_slots:
                problems.append(f"{roll}: two workshops in slot {slot}")
            seen_slots.add(slot)
            workshop = workshops_by_oid.get(oid)
            if workshop is None:
                problems.append(f"{roll}: workshop registration points at no known workshop")
            else:
                counts["workshop_popularity"][workshop.get("name")] += 1
                workshop_seats_added[oid] += 1
                if entry.get("attended"):
                    workshop_attendance_added[oid] += 1
                    workshops_attended += 1
                if entry.get("booking_type") == "on-spot":
                    workshops_on_spot += 1
                elif entry.get("booking_type") != "pre-registered":
                    problems.append(
                        f"{roll}: workshop booking_type {entry.get('booking_type')!r} is "
                        "neither 'pre-registered' nor 'on-spot'"
                    )

    # --- registrations sit inside the catalogue's own window ------------------
    # The rule used to be a fixed date ("nothing before 25 May"). It is now the
    # window the catalogue itself advertises, because that window is anchored to
    # now: a registration outside it is one the API would have refused with
    # "Registration is closed for this event".
    window_opens = REGISTRATION_OPENS.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest_registration = min(registration_times) if registration_times else None
    early = [when for when in registration_times if when < window_opens]
    if early:
        problems.append(
            f"{len(early)} registrations predate the registration window opening "
            f"{window_opens.date().isoformat()} (earliest {min(early)})"
        )
    late = [when for when in registration_times if when > seed_calendar.NOW]
    if late:
        problems.append(
            f"{len(late)} registrations are dated in the future (latest {max(late)})"
        )
    if len(registration_times) != event_registrations + workshop_registrations:
        problems.append(
            f"{len(registration_times)} registration timestamps for "
            f"{event_registrations + workshop_registrations} registrations"
        )
    # Distinct timestamps: identical ones would be the signature of a batch job
    # rather than of students choosing when to sign up.
    distinct_times = len(set(registration_times))

    # --- blocks and halls stay within capacity, rooms stay unique ------------
    for hostel_id, occupied in block_occupancy.items():
        block = hostels_by_id[hostel_id]
        sharing = int(block.get("sharing", 1) or 1)
        rooms = block.get("rooms") or []
        room_numbers = {str(room.get("room_number")) for room in rooms}
        # The allocator's real ceiling, not the stated capacity: a block whose
        # rooms cannot hold `capacity` residents fills up sooner than its
        # capacity suggests.
        ceiling = min(int(block.get("capacity", 0) or 0), sharing * len(rooms))
        if occupied > ceiling:
            problems.append(f"{hostel_id}: {occupied} students in {ceiling} beds")

        # Rooms are *shared* — up to `sharing` occupants each. The old check
        # flagged any room allotted twice, which was right only while room numbers
        # were being invented one per student.
        overfull = [
            room for room, count in block_rooms[hostel_id].items() if count > sharing
        ]
        if overfull:
            problems.append(
                f"{hostel_id}: {len(overfull)} room(s) hold more than {sharing} occupant(s)"
            )
        unknown = [room for room in block_rooms[hostel_id] if str(room) not in room_numbers]
        if unknown:
            problems.append(
                f"{hostel_id}: {len(unknown)} allotted room number(s) do not exist in the block"
            )
    for hall_oid, occupied in hall_occupancy.items():
        capacity = int(halls_by_oid[hall_oid].get("capacity", 0) or 0)
        if occupied > capacity:
            problems.append(
                f"{halls_by_oid[hall_oid].get('name')}: {occupied} diners for {capacity} seats"
            )

    # --- workshop seats: prior count plus what this run added has to fit ------
    sold_out = 0
    for workshop in workshops:
        prior = int(workshop.get("registration_count", 0) or 0)
        capacity = int(workshop.get("capacity", 0) or 0)
        booked = prior + workshop_seats_added[workshop["_id"]]
        if booked > capacity:
            problems.append(
                f"{workshop.get('name')}: {booked} registrations for {capacity} seats"
            )
        elif booked == capacity and capacity:
            sold_out += 1

    # --- every mirror landed, and agrees with the participant side ------------
    event_scan_rows = 0
    audit_rows = 0
    if written:
        seeded_ids = [doc["participant_id"] for doc in documents]
        with SessionLocal() as session:
            people = session.scalars(select(Participant).where(Participant.participant_id.in_(seeded_ids))).all()
            internal_ids = [p.id for p in people]
            event_scan_rows = session.scalar(select(func.count()).select_from(EventAttendance).where(
                EventAttendance.participant_id.in_(internal_ids))) or 0
            audit_rows = session.scalar(select(func.count()).select_from(SystemLog).where(
                SystemLog.actor_id.in_(seeded_ids),
                SystemLog.details["seed_source"].as_string() == SEED_MARKER)) or 0
            registration_rows = session.scalar(select(func.count()).select_from(WorkshopLog).where(
                WorkshopLog.participant_id.in_(internal_ids), WorkshopLog.action == "registration")) or 0
        if registration_rows != workshop_registrations:
            problems.append(
                f"workshop_logs holds {registration_rows} registration rows for "
                f"{workshop_registrations} participant-side registrations"
            )
        with SessionLocal() as session:
            attendance_rows = session.scalar(select(func.count()).select_from(WorkshopLog).where(
                WorkshopLog.participant_id.in_(internal_ids), WorkshopLog.action == "attendance")) or 0
        if attendance_rows != workshops_attended:
            problems.append(
                f"workshop_logs holds {attendance_rows} attendance rows for "
                f"{workshops_attended} participants marked attended"
            )

        for workshop in workshops:
            with SessionLocal() as session:
                registration_count = session.scalar(select(func.count()).select_from(WorkshopRegistration).where(
                    WorkshopRegistration.workshop_id == workshop["_id"], WorkshopRegistration.status == "registered")) or 0
                participant_count = session.scalar(select(func.count()).select_from(WorkshopAttendance).where(
                    WorkshopAttendance.workshop_id == workshop["_id"])) or 0
            expected_registrations = (
                int(workshop.get("registration_count", 0) or 0)
                + workshop_seats_added[workshop["_id"]]
            )
            if registration_count != expected_registrations:
                problems.append(
                    f"{workshop.get('name')}: registration_count is "
                    f"{registration_count}, expected {expected_registrations}"
                )
            expected_attendance = (
                int(workshop.get("participant_count", 0) or 0)
                + workshop_attendance_added[workshop["_id"]]
            )
            if participant_count != expected_attendance:
                problems.append(
                    f"{workshop.get('name')}: participant_count is "
                    f"{participant_count}, expected {expected_attendance}"
                )

        # Hostel occupancy: every allotted student must appear in their block's own
        # room, and `current_occupancy` must have moved by exactly as many.
        for hostel_id, seeded_here in block_occupancy.items():
            with SessionLocal() as session:
                hostel = session.scalar(select(Hostel).where(Hostel.hostel_id == hostel_id))
                mirrored = session.scalar(select(func.count()).select_from(AccommodationAllocation)
                    .join(HostelRoom, HostelRoom.id == AccommodationAllocation.room_id)
                    .where(HostelRoom.hostel_id == hostel.id,
                        AccommodationAllocation.participant_id.in_(internal_ids))) or 0 if hostel else 0
            if mirrored != seeded_here:
                problems.append(
                    f"{hostel_id}: {mirrored} seeded occupant(s) in the block's rooms for "
                    f"{seeded_here} students allotted to it"
                )
            expected_occupancy = (
                int(hostels_by_id[hostel_id].get("current_occupancy", 0) or 0) + seeded_here
            )
            if hostel.current_occupancy != expected_occupancy:
                problems.append(
                    f"{hostel_id}: current_occupancy is {hostel.current_occupancy}, "
                    f"expected {expected_occupancy}"
                )

    duplicates = {
        "roll_number": [k for k, v in rolls.items() if v > 1],
        "email": [k for k, v in emails.items() if v > 1],
        "phone": [k for k, v in phones.items() if v > 1],
        "photo": [k for k, v in photos.items() if v > 1],
    }
    for kind in ("roll_number", "email", "phone"):
        if duplicates[kind]:
            problems.append(f"{len(duplicates[kind])} duplicate {kind} values")

    stay = counts["stay"]
    if not (stay["both"] > stay["accommodation"] > stay["mess"]):
        problems.append(
            "accommodation/mess ordering broken: "
            f"both={stay['both']} accommodation={stay['accommodation']} mess={stay['mess']}"
        )
    if international >= INTERNATIONAL_CEILING + 1:
        problems.append(f"{international} international students — the ceiling is under 100")

    # The February 2026 programmes, spelled out as their own check rather than
    # left to the per-student level test, because it is the one restriction most
    # likely to be broken by a later edit to LEVEL_MIX.
    for code in ("MS", "AE"):
        bad = [
            doc for doc in documents
            if (doc.get("profile") or {}).get("degree_code") == code
            and (doc.get("profile") or {}).get("academic_level") != FOUNDATION
        ]
        if bad:
            problems.append(f"{len(bad)} {code} students are not at Foundation Level")

    return {
        "total": total,
        "profile_complete": profile_complete,
        "international": international,
        "counts": {key: dict(value) for key, value in counts.items()},
        "duplicates": {k: len(v) for k, v in duplicates.items()},
        "distinct_photos": len(photos),
        "hostel": {
            "registered": hostel_registered,
            "allotted": hostel_allotted,
            "pending": hostel_registered - hostel_allotted,
            "blocks_used": len(block_occupancy),
            "blocks_available": len(hostels),
            "occupancy": {hid: count for hid, count in sorted(block_occupancy.items())},
            "inside": residents_inside,
            "departed": residents_departed,
            "paid": hostel_payments,
        },
        "mess": {
            "registered": mess_registered,
            "allotted": mess_allotted,
            "pending": mess_registered - mess_allotted,
            "occupancy": {
                str(halls_by_oid[oid].get("name")): count
                for oid, count in hall_occupancy.items()
            },
            "scans": mess_scans_recorded,
            "paid": mess_payments,
        },
        "event_registrations": event_registrations,
        "event_scan_rows": event_scan_rows,
        "audit_rows": audit_rows,
        "workshop_registrations": workshop_registrations,
        "workshops_attended": workshops_attended,
        "workshops_on_spot": workshops_on_spot,
        "events_in_catalogue": len(events),
        "workshops_in_catalogue": len(workshops),
        "workshops_sold_out": sold_out,
        "earliest_registration": earliest_registration.isoformat() if earliest_registration else None,
        "distinct_registration_times": distinct_times,
        "mirrors_checked": written,
        "problems": problems,
    }


def _bucket(count: int) -> str:
    """Registration counts as reportable bands."""
    if count == 0:
        return "0"
    if count == 1:
        return "1"
    if count <= 3:
        return "2–3"
    if count <= 6:
        return "4–6"
    return "7+"


def print_report(report: dict[str, Any], log=print) -> None:
    """The validation report the brief asks for: actual counts, not target ratios."""
    total = report["total"] or 1
    counts = report["counts"]

    def section(title: str) -> None:
        log(f"\n{title}\n{'-' * len(title)}")

    def rows(
        data: dict,
        *,
        sort_by_value: bool = True,
        limit: int | None = None,
        of: int | None = None,
    ) -> None:
        """
        One line per key: count, then share.

        ``of`` overrides the denominator, because not every table is a share of
        the whole cohort — meal preference is a share of the students who took
        mess, and printing it against the total would read as though most of the
        fest had no dietary preference recorded.
        """
        denominator = of if of is not None else total
        items = sorted(
            data.items(),
            key=(lambda kv: -kv[1]) if sort_by_value else (lambda kv: str(kv[0])),
        )
        if limit:
            items = items[:limit]
        width = max((len(str(k)) for k, _ in items), default=0)
        for key, value in items:
            log(f"  {str(key):<{width}}  {value:>6}  {value / max(denominator, 1) * 100:5.1f}%")

    log("\n" + "=" * 78)
    log(f"PARADOX STUDENT SEED — VALIDATION REPORT")
    log("=" * 78)
    log(f"\nTotal students: {report['total']}   "
        f"(profiles complete: {report['profile_complete']})")

    section("Gender")
    rows(counts["gender"])
    male = counts["gender"].get("male", 0)
    female = counts["gender"].get("female", 1)
    log(f"  ratio male:female = {male / max(female, 1):.2f} : 1   (target 1.50 : 1)")

    section("Degree")
    rows(counts["degree"])
    by_code = {
        DEGREES[c].name: counts["degree"].get(DEGREES[c].name, 0) for c in ("DS", "ES", "MS", "AE")
    }
    smallest = max(min(by_code.values()), 1)
    log("  ratio DS:ES:MS:AE = "
        + " : ".join(f"{v / smallest:.2f}" for v in by_code.values())
        + "   (target 9 : 4 : 2 : 1)")

    section("Entry year")
    rows(counts["entry_year"], sort_by_value=False)

    section("Entry term (the 1/2/3 in the roll number)")
    rows(counts["entry_term"], sort_by_value=False)

    section("Academic level")
    rows(counts["level"])

    section("Course stage (canonical field)")
    rows(counts["course_stage"])

    section("Degree type")
    rows(counts["degree_type"])

    section("Nationality")
    rows(counts["nationality"])
    log(f"  international (living outside India): {report['international']}")

    section("International countries")
    rows(counts["country"])

    section("Age bands")
    rows(counts["age_band"], sort_by_value=False)

    section("Houses")
    rows(counts["house"])

    section("Accommodation and mess")
    stay = counts["stay"]
    log(f"  both accommodation + mess   {stay.get('both', 0):>6}")
    log(f"  accommodation only          {stay.get('accommodation', 0):>6}")
    log(f"  mess only                   {stay.get('mess', 0):>6}")
    log(f"  neither                     {stay.get('neither', 0):>6}")
    ordering = "holds" if stay.get("both", 0) > stay.get("accommodation", 0) > stay.get("mess", 0) else "BROKEN"
    log(f"  required ordering both > accommodation only > mess only: {ordering}")
    log(f"  hostel: {report['hostel']['registered']} requested, "
        f"{report['hostel']['allotted']} allotted across "
        f"{report['hostel']['blocks_used']} of {report['hostel']['blocks_available']} blocks, "
        f"{report['hostel']['pending']} awaiting allocation")
    log(f"          {report['hostel']['inside']} currently inside, "
        f"{report['hostel']['departed']} permanently departed, "
        f"{report['hostel']['paid']} paid the fee")
    log(f"  mess:   {report['mess']['registered']} requested, "
        f"{report['mess']['allotted']} allotted, {report['mess']['pending']} awaiting allocation")
    log(f"          {report['mess']['scans']} meal scans recorded, "
        f"{report['mess']['paid']} paid the fee")
    if report["mess"]["occupancy"]:
        log("  hall occupancy: " + ", ".join(
            f"{name} {count}" for name, count in sorted(report["mess"]["occupancy"].items())
        ))

    mess_takers = stay.get("both", 0) + stay.get("mess", 0)
    section(f"Meal preference (of the {mess_takers} students who took mess)")
    rows({k: v for k, v in counts["mess_preference"].items() if k}, of=mess_takers)

    section("Event registrations")
    log(f"  total registrations: {report['event_registrations']} "
        f"across {report['events_in_catalogue']} events")
    log(f"  attendance scan rows: {report['event_scan_rows']}")
    log(f"  per student:")
    rows(counts["events_per_student"], sort_by_value=False)
    popularity = counts["event_popularity"]
    if popularity:
        ordered = sorted(popularity.items(), key=lambda kv: -kv[1])
        log(f"  busiest: " + ", ".join(f"{n} ({c})" for n, c in ordered[:5]))
        log(f"  quietest: " + ", ".join(f"{n} ({c})" for n, c in ordered[-5:]))
        log(f"  spread: {ordered[-1][1]}–{ordered[0][1]} registrations per event")

    section("Workshop registrations")
    log(f"  total registrations: {report['workshop_registrations']} "
        f"across {report['workshops_in_catalogue']} workshops")
    log(f"  attended: {report['workshops_attended']}   "
        f"booked at the door: {report['workshops_on_spot']}")
    log(f"  per student:")
    rows(counts["workshops_per_student"], sort_by_value=False)
    popularity = counts["workshop_popularity"]
    if popularity:
        ordered = sorted(popularity.items(), key=lambda kv: -kv[1])
        log(f"  busiest: " + ", ".join(f"{n[:40]} ({c})" for n, c in ordered[:4]))
        log(f"  quietest: " + ", ".join(f"{n[:40]} ({c})" for n, c in ordered[-4:]))
        log(f"  spread: {ordered[-1][1]}–{ordered[0][1]} registrations per workshop")

    section("Photos")
    log(f"  distinct photo URLs: {report['distinct_photos']} for {report['total']} students")
    rows(counts["photo_kind"])

    section("Checks")
    log(f"  audit rows written: {report['audit_rows']}")
    if report["earliest_registration"]:
        total_registrations = report["event_registrations"] + report["workshop_registrations"]
        log(f"  earliest registration timestamp: {report['earliest_registration']} "
            f"(window opens {REGISTRATION_OPENS.date().isoformat()})")
        log(f"  distinct registration timestamps: "
            f"{report['distinct_registration_times']} of {total_registrations}")
    log(f"  workshops sold out: {report['workshops_sold_out']} "
        f"of {report['workshops_in_catalogue']}")
    log("  catalogue mirrors cross-checked against the participant side: "
        + ("yes" if report["mirrors_checked"] else "no (dry run — nothing written)"))
    for kind, count in report["duplicates"].items():
        log(f"  duplicate {kind}s: {count}")
    if report["problems"]:
        log(f"\n  FAILED — {len(report['problems'])} problem(s):")
        for problem in report["problems"][:25]:
            log(f"    • {problem}")
        if len(report["problems"]) > 25:
            log(f"    … and {len(report['problems']) - 25} more")
    else:
        log("\n  PASSED — every consistency rule held.")
    log("")


# =============================================================================
# CLI
# =============================================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed a realistic Paradox student population.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--total", type=int, default=None,
        help=f"target cohort size (default ~{DEFAULT_TOTAL}); a target, not a quota",
    )
    parser.add_argument("--seed", type=int, default=None, help="RNG seed, for a reproducible run")
    parser.add_argument(
        "--password", default="Paradox@2026",
        help="password every seeded student signs in with (default: Paradox@2026)",
    )
    parser.add_argument(
        "--keys", type=int, default=16,
        help="size of the RSA QR keypair pool cycled across the cohort (default: 16)",
    )
    parser.add_argument(
        "--embeddings", action="store_true",
        help="call the embeddings provider per student instead of storing zero vectors",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="generate and validate without writing anything",
    )
    parser.add_argument(
        "--wipe", action="store_true",
        help="remove students a previous run of this script wrote, then seed again",
    )
    parser.add_argument(
        "--wipe-only", action="store_true",
        help="remove students a previous run of this script wrote, and stop",
    )
    parser.add_argument(
        "--fix-house-names", action="store_true",
        help=(
            "rewrite any already-seeded participant's suffixed house name "
            "('Bandipur House') to the bare form models.HOUSES expects, then stop"
        ),
    )
    parser.add_argument(
        "--demo-catalogue", action="store_true",
        help="fill empty event/workshop/hostel/mess collections from the frontend datasets",
    )
    parser.add_argument("--report-json", type=Path, default=None, help="also write the report as JSON")
    args = parser.parse_args()

    validate_cohort_plan()

    seed = args.seed if args.seed is not None else random.randrange(1, 10_000_000)
    rng = random.Random(seed)

    print(f"Paradox student seed — rng seed {seed}"
          + ("  [dry run]" if args.dry_run else ""))

    if args.fix_house_names:
        print("\nFixing suffixed house names…")
        if args.dry_run:
            with SessionLocal() as session:
                preview = sum(session.scalar(select(func.count()).select_from(Participant).where(
                    Participant.profile["house"].as_string() == suffixed)) or 0
                    for suffixed in HOUSE_NAME_FIXUPS)
            print(f"  would fix {preview} participant(s)")
        else:
            fix_house_names()
        return 0

    if args.wipe or args.wipe_only:
        print("\nRemoving previously seeded students…")
        if args.dry_run:
            with SessionLocal() as session:
                count = session.scalar(select(func.count()).select_from(Participant).where(
                    Participant.attributes["seed_source"].as_string() == SEED_MARKER)) or 0
            print(f"  would remove {count} seeded participants")
        else:
            wipe_seeded()
        if args.wipe_only:
            return 0

    if args.demo_catalogue and not args.dry_run:
        seed_demo_catalogue()

    catalogues = load_catalogues()
    if args.demo_catalogue and args.dry_run:
        catalogues = {
            kind: items or in_memory_catalogue(kind) for kind, items in catalogues.items()
        }
    missing = [name for name in ("events", "workshops", "hostels", "mess") if not catalogues[name]]
    if missing:
        print(
            "\nMissing catalogue data: " + ", ".join(missing)
            + "\nSeed them first (seed.py, seed_mess.py, seed_events.py, seed_workshops.py),"
            " or pass --demo-catalogue to load the frontend datasets into an empty database."
        )
        return 1
    print(f"  catalogue: {len(catalogues['events'])} events, "
          f"{len(catalogues['workshops'])} workshops, "
          f"{len(catalogues['hostels'])} hostels, {len(catalogues['mess'])} mess halls")

    print("\nGenerating students…")
    students = build_population(rng, args.total, read_existing())
    print(f"  {len(students)} students")

    assign_stay(rng, students)
    placed_rooms = allocate_hostels(rng, students, catalogues["hostels"])
    placed_mess = allocate_mess(rng, students, catalogues["mess"])
    print(f"  {placed_rooms} rooms allotted, {placed_mess} mess seats allotted")

    scans = assign_mess_scans(rng, students, catalogues["mess"])
    mess_paid, hostel_paid = assign_payments(rng, students)
    print(f"  {scans} meal scans, {mess_paid} mess fees and {hostel_paid} hostel fees settled")

    assign_event_registrations(rng, students, catalogues["events"])
    assign_workshop_registrations(rng, students, catalogues["workshops"])
    print(f"  {sum(len(s.events) for s in students)} event registrations "
          f"({sum(len(s.event_scans) for s in students)} scanned), "
          f"{sum(len(s.workshops) for s in students)} workshop registrations "
          f"({sum(len(s.workshop_attendance) for s in students)} attended)")

    print(f"\nHashing password and generating {args.keys} QR keypairs…")
    password_hash = get_password_hash(args.password)
    key_pool = [generate_rsa_key_pair() for _ in range(max(1, args.keys))]

    # Unique per run, not just per second: the read-back below selects on this,
    # and two seed runs inside the same second would otherwise share a tag and
    # validate each other's documents.
    batch = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}-{uuid.uuid4()}"
    documents = []
    for index, student in enumerate(students):
        embedding = (
            generate_embedding(student.event_preferences) if args.embeddings else zero_embedding()
        )
        documents.append(
            to_document(student, password_hash, key_pool[index % len(key_pool)], embedding, batch)
        )

    if args.dry_run:
        print("\nDry run — nothing written.")
        report_documents = documents
    else:
        print("\nWriting…")
        write_students(students, documents)
        report_documents = documents
        print(f"  wrote {len(report_documents)} participants and relational activity rows")

    registration_times = [
        when
        for student in students
        for when in [t for _, t in student.event_logs] + [t for _, t in student.workshop_logs]
    ]
    report = build_report(
        report_documents,
        catalogues["events"],
        catalogues["workshops"],
        catalogues["hostels"],
        catalogues["mess"],
        registration_times,
        written=not args.dry_run,
    )
    report["rng_seed"] = seed
    report["dry_run"] = args.dry_run
    print_report(report)

    if args.report_json:
        args.report_json.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"Report written to {args.report_json}")

    print(f"Students sign in with their institutional email and: {args.password}")
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
