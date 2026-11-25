# Paradox Connect

A Centralized Management Platform & Student Help Portal for **Paradox**, the IIT Madras fest.

Software Engineering [BSCS3001] — IIT Madras BS Degree Program
**Team:** blastoi-SE · **Team Code:** MAY2026-Team-079

## Overview

Paradox Connect is a Progressive Web App (PWA) that replaces scattered email/WhatsApp
communication and manual physical verification (ID cards, mess cards, attendance sheets)
with one integrated platform, built around a single participant profile. It covers:

- Centralized event information and updates
- A unified QR/TOTP-based digital identity for all entry checkpoints
- Real-time attendance and crowd visibility
- Mess information and digital mess access
- Hostel allocation and digital check-in
- Hostel and mess fee payments (via a certified third-party gateway)
- Structured query and contact management
- Targeted announcements and an operational dashboard for the core team

## Tech Stack

| Layer          | Choice                                                              |
|----------------|--------------------------------------------------------------------|
| Frontend       | React.js (Progressive Web App)                                      |
| Backend        | Python + FastAPI                                                    |
| Database       | **PostgreSQL** (SQLAlchemy ORM, Alembic migrations)                 |
| Authentication | Email + password (IITM domains only), bcrypt, JWT sessions          |
| Digital ID     | Rotating QR, RSA-OAEP encrypted payload, per participant             |
| Payments       | Mock gateway (see Project Status)                                   |
| Hosting        | Vercel (frontend) · Render (backend) — free tier                   |

> **Database note:** the backend uses PostgreSQL exclusively. Set `DATABASE_URL`
> to a PostgreSQL connection string and apply Alembic migrations before starting
> the API.

See [the PostgreSQL data model](docs/database.md) for the relational schema,
relationships, constraints, and migration workflow.

## Project Status

**Sprint 3 · 13–18 August 2026** — the Milestone 2 screens are now integrated against the
live FastAPI backend, and the staff/admin console has been built out on top of it.

| Area | State |
|---|---|
| Public site — landing, event catalogue, workshop programme, schedule, sponsors | Built |
| Participant app — registration, dual login, profile, event & workshop booking | Built |
| Data source | **Live backend only.** The in-memory mock API was removed; the app always calls the real FastAPI backend. |
| Digital identity — rotating QR, RSA-OAEP payload (replaces the earlier TOTP scheme) | Built |
| Checkpoint scanners — event, workshop, mess, hostel | Built |
| Staff console — staff home, event teams, event participation | Built |
| Admin console — events, workshops, mess, hostels, staff, backend teams | Built |
| Audit trail — full log plus per-entity views | Built |
| Fest overview board — KPIs, capacity, trends, alerts | Built |
| Mess & hostel fee payments | **Mock gateway.** `POST /hostels/pay` and `POST /mess/pay` record a simulated settlement (fixed server-side fee, no real gateway or money involved) and the frontend now settles against these routes instead of a client-side demo ledger. |
| Query / contact management | **Not started** |
| Targeted announcements | **Not started** |

The backend gained a participant statistics endpoint, unauthenticated event and workshop
catalogues, accommodation request/cancel routes, mess cuisine metadata, and audit-log
filtering this sprint, with seed scripts and pytest coverage for each.

## Documentation

- [Product Requirements Document](/docs/Paradox_Connect_PRD.md) — full scope, functional
  requirements, NFRs, risks, and release plan.

## Team

| Name                   | Roll No.   | Role                                |
|------------------------|------------|-------------------------------------|
| Veer Shah              | 23f1001524 | Code Reviewer / Tester              |
| Ashwin Devi Srinivasan | 23f2000226 | Backend Developer                   |
| Anshuman Pandey        | 23f3001726 | Product Manager / Backend Developer |
| Tanisha Agrawal        | 23f3001897 | Scrum Master / Tester               |
| Ravi Kumar K           | 24f1002594 | Frontend Developer                  |

## Running the Application

The app has two parts that both need to be running at once: the FastAPI backend
and the React frontend. The backend requires PostgreSQL.

### Prerequisites

- **Python 3.10+** and **pip**
- **Node.js** (current LTS) and **npm**
- **PostgreSQL 14+** installed and running locally or a reachable PostgreSQL service

### 1. Start PostgreSQL

Create the `paradox` database and start the PostgreSQL service:

```bash
# macOS (Homebrew)
brew services start postgresql@16
```

Confirm it's up:

```bash
pg_isready -h localhost -p 5432
```

### 2. Set up a Python virtual environment (backend)

A `requirements.txt` is provided at the **repository root** so you can create one
virtual environment and install everything the backend needs without first
`cd`-ing into `backend/`:

```bash
# from the repository root
python -m venv venv

# activate it
source venv/bin/activate      # macOS/Linux
venv\Scripts\activate         # Windows

# install backend dependencies
pip install -r requirements.txt
```

### 3. Configure the backend environment

Copy `.env.example` to `.env` in the repository root and set credentials for
your local PostgreSQL instance:

```env
DATABASE_URL=postgresql+psycopg://paradox:change-me@localhost:5432/paradox
SECRET_KEY=replace-with-a-long-random-secret
```

This file is gitignored and must never be committed.

### 4. Run the backend

With the virtual environment active and PostgreSQL running:

```bash
cd backend
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

The API is now live at `http://127.0.0.1:8000` (docs at `/docs`). See the
detailed guide below for seeding the database with hostels, mess halls, events,
and workshops if you're starting from an empty database.

### 5. Run the frontend

In a separate terminal, with Node/npm installed:

```bash
cd frontend
cp .env.example .env.local   # first time only — sets VITE_API_BASE_URL
npm install
npm run dev
```

Vite prints the local dev server URL (typically `http://localhost:5173`). Open
it in a browser; the app calls the backend at the `VITE_API_BASE_URL` set in
`frontend/.env.local` (defaults to `http://localhost:8000`).

At this point both servers are running against the local PostgreSQL database, and
the app is fully usable end to end.

## Backend Setup and Deployment Guide

This comprehensive guide covers how to set up and run the Paradox Connect backend from scratch, including environment configuration, database setup, seeding, and deployment.

### Free demo deployment

The repository includes a Render Blueprint for the FastAPI API (`render.yaml`) and a
Vercel SPA rewrite (`frontend/vercel.json`). Deploy the frontend as a Vercel static
site with the project root set to `frontend`, build command `npm run build`, and
output directory `dist`. Set `VITE_API_BASE_URL` to the deployed Render API URL
without a trailing slash. Deploy the backend from this repository using the Render
Blueprint, and set `DATABASE_URL` to a PostgreSQL connection URL and `CORS_ORIGINS`
to the exact Vercel origin (for example, `https://your-project.vercel.app`). Render
generates `SECRET_KEY` from the Blueprint. The API runs Alembic migrations during
build and exposes `/health` as a database readiness check.

The current demo uses Render's free PostgreSQL instance, which expires on December
25, 2026. Render's free web services sleep after 15 minutes of inactivity, so the
first API request after idle can be slow. Treat this as a temporary demonstration:
free-plan limits, no uptime SLA, and the database expiry make it unsuitable for
dependable production data. Never seed real participant data into a public demo.

Payments remain simulated; no money is moved. Do not describe the deployed demo as
accepting real payments.

---

### **Prerequisites**

- **Python 3.8+**
- **Git** (for cloning repository)
- **pip** (Python package manager)

---

### **1. Clone and Setup**

```bash
git clone <repository-url>
cd MAY2026-Team-079/backend
```

---

### **2. Python Environment**

```bash
# Create virtual environment (recommended)
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

### **3. Database Configuration**

The application uses PostgreSQL through SQLAlchemy and psycopg. Set `DATABASE_URL`
and apply the Alembic migration from `backend/`:

```bash
cd backend
alembic upgrade head
```

---

### **4. Environment Variables**

Create `.env` in the repository root:

```env
# Database Configuration
DATABASE_URL=postgresql+psycopg://paradox:change-me@localhost:5432/paradox

# Authentication
SECRET_KEY=replace-with-a-long-random-secret

# AI Embeddings (choose one option)

# Option 1: OpenAI API (official, paid)
OPENAI_API_KEY=sk-your-openai-api-key-here

# Option 2: Local Embeddings (free, local server)
# EMBEDDINGS_API_KEY=not-needed
# EMBEDDINGS_BASE_URL=http://localhost:11434/v1  # Ollama default
# EMBEDDINGS_MODEL=llama2  # or your local model name

# Optional: Seeding passwords (will prompt if not set)
PARADOX_ADMIN_PASSWORD=admin123
PARADOX_STAFF_PASSWORD=staff123
```

**Important**: Never commit this file to git. It's already in `.gitignore`.

---

### **5. Running the Backend**

```bash
# Development mode (auto-reload)
python main.py

# OR using uvicorn directly
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Verify server is running:
- API Documentation: http://localhost:8000/docs
- OpenAPI Spec: http://localhost:8000/openapi.json

---

### **6. Database Seeding (Complete Setup)**

**Run these commands in order from the `backend/` directory:**

```bash
# 1. Bootstrap the first Super Admin account
python seed_staff.py --bootstrap --roster

# 2. Seed hostels catalogue
python seed.py --email paradox.admin@example.com

# 3. Seed mess halls and menus
python seed_mess.py --email paradox.admin@example.com

# 4. Seed events catalogue
python seed_events.py --email paradox.admin@example.com

# 5. Seed workshops catalogue
python seed_workshops.py --email paradox.admin@example.com

# 6. Assign staff to facilities
python seed_staff.py --assign --email paradox.admin@example.com

# 7. Seed student population (3000+ realistic students)
python seed_students.py
```

**Alternative: Dry run first**
```bash
python seed_students.py --dry-run
python seed.py --dry-run --email paradox.admin@example.com
```

---

### **7. Testing the Setup**

#### **Verify Seeding**
```bash
# Check counts in database
psql "$DATABASE_URL" -c 'SELECT count(*) FROM participants;'
psql "$DATABASE_URL" -c 'SELECT count(*) FROM staff_users;'
psql "$DATABASE_URL" -c 'SELECT count(*) FROM events;'
```

#### **Test Authentication**
```bash
# Admin login
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"paradox.admin@example.com","password":"admin123"}'

# Student login (use seeded student)
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"DS26F1000001@example.com","password":"password"}'
```

---

### **8. Production Deployment**

#### **Environment Variables in Production**
```bash
# Use system environment variables or secrets manager
export SECRET_KEY=$(openssl rand -hex 32)
export DATABASE_URL="postgresql+psycopg://user:password@host:5432/paradox"
export OPENAI_API_KEY="your-production-key"
```

#### **Running as Service (Linux)**
Create `/etc/systemd/system/paradox-backend.service`:
```ini
[Unit]
Description=Paradox Connect Backend
After=network.target postgresql.service

[Service]
User=paradox
WorkingDirectory=/opt/paradox/backend
Environment="PATH=/opt/paradox/venv/bin"
ExecStart=/opt/paradox/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

### **9. Troubleshooting**

#### **Database Connection Failed**
```bash
# Check PostgreSQL is running
sudo systemctl status postgresql
pg_isready
```

#### **Missing Environment File**
```bash
# Create if missing
cp .env.example .env
# Edit with your values
```

#### **Seeding Errors**
```bash
# Check if catalogues exist first
python -c "from database import events_collection; print('Events:', events_collection.count_documents({}))"

# Run with verbose output
python seed_students.py --verbose
```

---

### **10. Default Credentials**

- **Super Admin**: `paradox.admin@example.com` / `admin123`
- **Staff Accounts**: Use passwords set in `PARADOX_STAFF_PASSWORD`
- **Students**: `DS26F1000001@example.com` through `DS26F1003000@example.com` / `password`

---

### **Quick Start Script**

Create `setup.sh`:
```bash
#!/bin/bash
cd backend

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cat > ../.env << EOF
DATABASE_URL=postgresql+psycopg://paradox:change-me@localhost:5432/paradox
SECRET_KEY=$(openssl rand -hex 32)
OPENAI_API_KEY=your-key-here
EOF

alembic upgrade head
uvicorn main:app --host 0.0.0.0 --port 8000 --reload &
```

---

### **API Documentation**

Complete API documentation is available at:
- Interactive Swagger UI: http://localhost:8000/docs
- OpenAPI JSON: http://localhost:8000/openapi.json
- API Specification: [/api_documentation.yaml](/api_documentation.yaml)
