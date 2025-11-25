# Centralized Attendance Platform

A lightweight, centralized attendance management app for university courses, built with Streamlit + SQLAlchemy.
Designed for the Harokopio University of Athens (HUA) postgraduate and undergraduate courses, with an easy QR-based workflow.

## Overview

| Role                          | Key Capabilities                                                                                                               |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| 👩‍🏫 **Instructor**          | Open/close sessions for each lesson, auto-generate QR codes, track who checked in, download per-session or per-period reports. |
| 🧑‍💼 **Admin / Secretaires** | Manage users, courses, and instructor assignments. View and export attendance data for all courses.                            |
| 👨‍🎓 **Student**             | Scan QR code during the lecture and submit name + HUA email. Attendance is logged automatically.                               |
| 🛠️ **Developer**            | Easily customizable and extendable codebase, built with popular open-source libraries.                                          |

## Architecture

- **Frontend**: Streamlit

- **Backend**: SQLAlchemy ORM (SQLite or PostgreSQL)

- **Deployment**: Docker / Docker Compose

- **Data Export**: CSV download (per session, per day/week/month, or overall)

- **Auth**: Local demo (passcode); can integrate Google OAuth / SSO in production

## Quick Start (Local)

1. Clone the repo:

   ```bash
   git clone git@github.com:gfragi/attendance-app.git
    cd attendance-app
    ```

2. Create `.env.dev`
    ```bash
    cp .env.dev.example .env.dev
    ```

3. Run with Docker Compose:

   ```bash
   docker-compose -f docker-compose.dev.yml up --build
   ```

   → Open http://localhost:8080 in your browser.

4. Default Demo Users

- Admin: admin@example.com
- Instructor: instructor@example.com
- Default Course: PTIA-101 — Προηγμένες Τεχνολογίες Πληροφορικής

## Instructor Guide

### 1. Login (demo mode)

Enter your instructor email (e.g., gfragi@hua.gr) and passcode (any string for now).
In production, authentication will go through HUA’s Google SSO.

### 2. Manage Attendance Sessions

Each course you’re assigned appears in your dropdown.

1. Choose your course
2. Set a duration (default: 15 minutes)
3. Click “Open new attendance session”

This automatically creates a new session:

- Generates a unique URL + QR code
- Valid for the specified duration
- Students can check in only during that window

### 3. Students Check-In

1. Scan the QR code displayed in class
2. Fill in their full name and HUA email
3. Attendance instantly appears in your dashboard

### 4. Close or Extend the Session

After attendance collection:

1. Close session (prevents new check-ins)
2. Extend by 10 min (if needed)
3. Confirm number of submissions vs. actual attendees

## 5. View Reports

Under 📊 Instructor Reports:

- Filter by course and date range
- Group by day, week, or month
- See raw check-ins, aggregated counts, and student attendance rates
- Download all tables as CSV

Example views:

- Total check-ins per day
- Unique students per course
- Attendance rate % per student

## Admin / Secretaire Guide

Accessible from the Admin Panel tab.

### 0. Roles & Access Control

- **Admins** are bootstrapped from the environment variable `ADMIN_EMAILS`.
  On startup, these emails are synced into the `users` table with role `admin`.
- **Instructors** are managed in the database (`users.role = instructor`).
  Add instructors via:
  1) Admin Panel → User Management, or  
  2) Bulk Import (CSV).

Environment instructor lists (if present) are used only for optional initial seeding.
The database remains the source of truth for instructor access.

### 1. Manage Users

Add new:

- Admins – full platform access
- Instructors – assignable to courses

### 2. Manage Courses

Add new courses (e.g., EFP01 — Software Development I).

### 3. Assign Instructors

Link instructors to courses using the dropdown menus.

### 4. Bulk Import (CSV)

The Bulk Import expects 4 fields:

| Field | Accepted headers |
|------|------------------|
| Course code | `course_code`, `course code`, `id` |
| Course title | `course_title`, `course title`, `corse title` |
| Instructor name | `instructor_name`, `instructor name`, `professor` |
| Instructor email | `instructor_email`, `instructor email`, `email` |

Example CSV:

```csv
id,corse title,professor,email
ΠΜΣ1-1,ΣΤΑΤΙΣΤΙΚΗ ΚΑΙ ΟΠΤΙΚΟΠΟΙΗΣΗ ΔΕΔΟΜΕΝΩΝ, <instructor name>, <instructor email>
ΠΜΣ1-2,ΜΗΧΑΝΙΚΗ ΜΑΘΗΣΗ, <instructor name>, <instructor email>
```

### 5. Global Reports

Under Reports (Admin):

- Filter by date range and course(s)
- Group results per day, week, or month
- Export aggregated, pivot, and per-student CSVs

Useful for:

- Generating attendance summaries for each instructor/course
- Comparing participation trends across time
- Checking compliance with attendance requirements


## Data Model

| Table                | Description                                                |
| -------------------- | ---------------------------------------------------------- |
| `users`              | Admins and instructors                                     |
| `courses`            | Course info (code, title)                                  |
| `course_instructors` | Instructor–course assignments                              |
| `sessions`           | Attendance sessions (open/closed, duration, token, expiry) |
| `attendance`         | Individual student check-ins (name, email, timestamp)      |


🔐 Authentication & Security Notes

- In demo mode, instructor/admin login uses simple passcodes.

- For production:
    - Add Google OAuth2 / institutional SSO via a reverse proxy.
    - Enforce email domain validation (@hua.gr).
    - Limit session validity (e.g., 15 minutes max).
    - Optional: Add CAPTCHA or one-time QR link tokens.
    - Configure HTTPS using Caddy or Nginx reverse proxy.


## 🐳 Deployment with Docker

### Development

```bash
docker-compose -f docker-compose.dev.yml up --build
```

- Uses SQLite for simplicity.
- Auto-reloads with local volume mounts

### Production

```bash
docker-compose -f docker-compose.prod.yml up --build -d
```

- Uses PostgreSQL
- Configure environment variables in `.env.prod`
- Accessible via reverse proxy (HTTPS)


### Common Environment Variables

| Variable                  | Description                          | Example                                                    |
| ------------------------- | ------------------------------------ | ---------------------------------------------------------- |
| `PUBLIC_BASE_URL`         | Base URL used for QR code generation | `https://attendance.hua.gr`                                |
| `EMAIL_DOMAIN`            | Allowed email domain for check-ins   | `@hua.gr`                                                  |
| `DATABASE_URL`            | Connection string                    | `postgresql+psycopg2://user:pass@postgres:5432/attendance` |
| `SESSION_DEFAULT_MINUTES` | Default session duration             | `15`                                                       |
| `STREAMLIT_SERVER_PORT`   | Internal port                        | `8501`                                                     |

## Example Workflow

| Time         | Actor                | Action                                     |
| ------------ | -------------------- | ------------------------------------------ |
| 18:00        | Instructor           | Opens session for *Software Development I* |
| 18:01        | Students             | Scan QR & submit attendance                |
| 18:15        | Session auto-expires |                                            |
| 18:20        | Instructor           | Downloads attendance CSV                   |
| End of month | Admin                | Generates global report grouped by week    |


