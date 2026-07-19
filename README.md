# Exam OS

Full-stack competitive exam (CBT) platform — SSC / Banking style mocks.

> Frontend upgrades are deferred; backend is production-hardened and runnable today.

---

## Quick start

### Backend

```bash
cd backend
python -m venv .venv

# Windows
# .venv\Scripts\activate

# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # optional — defaults work for local SQLite
python app.py


cd D:\documents\cronyzo_test\exam-os\backend

.\.venv\Scripts\Activate.ps1

python app.py


cd D:\documents\cronyzo_test\exam-os\frontend

npm run dev

```

- API: http://127.0.0.1:5000  
- Health: http://127.0.0.1:5000/api/health  

### Frontend (later)

```bash
cd frontend
npm install
npm run dev
```

App: http://127.0.0.1:5173 (proxies `/api` → backend)

### Production-style API (gunicorn)

```bash
cd backend
source .venv/bin/activate
export FLASK_ENV=production
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export JWT_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
gunicorn -b 0.0.0.0:5000 -w 2 -k gthread --threads 4 "app:app"
```

---

## Demo logins

| Role    | Email                | Password   |
|---------|----------------------|------------|
| Admin   | admin@examos.local   | admin123   |
| Student | student@examos.local | student123 |

Seeded automatically on first boot (idempotent).

---

## Database

**Default:** SQLite file `backend/exam_os.db` (created on start).

**Neon / PostgreSQL:**

```bash
export DATABASE_URL="postgresql://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require"
# optional driver:
pip install "psycopg[binary]>=3.2.0"
python app.py
```

Also accepts `NEON_DATABASE_URL`. Scheme `postgres://` is normalized to `postgresql://`.

### Migrations (Flask-Migrate)

```bash
cd backend
export FLASK_APP=app:create_app
flask db migrate -m "describe change"
flask db upgrade
```

Runtime also calls `db.create_all()` for zero-config local boot.

---

## API surface (stable)

| Prefix | Purpose |
|--------|---------|
| `/api/health` | Liveness |
| `/api/auth` | Register, login, refresh, profile |
| `/api/subjects` | Subject CRUD |
| `/api/chapters` | Chapter CRUD |
| `/api/questions` | Question bank |
| `/api/exams` | Exam builder, assign, publish |
| `/api/attempts` | Start, answer, submit, result, review |
| `/api/analytics` | Student / admin dashboards |
| `/api/admin` | User management |
| `/api/imports` | CSV / JSON bulk questions |

---

## Features

- JWT auth — roles `admin` / `student`
- Subject / chapter / question CRUD
- Question types: single, multi, integer, paragraph, image, math
- Exam builder, section assign, publish
- CSV + JSON import (size/row capped)
- Exam player backend: timer expiry, palette state, mark-for-review, autosave, security events
- Results + question-wise review
- Analytics dashboards
- Security headers, request IDs, safe error envelopes
- SQLite ↔ Neon auto switch via env

---

## Environment

See `backend/.env.example` for the full list (`SECRET_KEY`, `JWT_*`, `CORS_ORIGINS`, `DATABASE_URL`, feature flags, pool knobs).

---

## Project layout

```
exam-os/
├── backend/
│   ├── app.py                 # entrypoint
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── migrations/
│   └── app/
│       ├── config.py
│       ├── extensions.py
│       ├── models/
│       ├── routes/
│       ├── services/
│       └── utils/
└── frontend/                  # Vite + React (upgrade later)
```
