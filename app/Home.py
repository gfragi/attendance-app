import os
import io
import uuid
import base64
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import qrcode

# -----------------------------
# Config (prod-ish)
# -----------------------------
APP_TITLE = "Centralized Attendance for University Courses"

# Domain & durations
EMAIL_DOMAIN = os.getenv("EMAIL_DOMAIN", "@hua.gr")
SESSION_DEFAULT_MINUTES = int(os.getenv("SESSION_DEFAULT_MINUTES", "15"))

# DB (SQLite by default; in containers use /data)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/attendance.db")
engine = create_engine(DATABASE_URL, echo=False, future=True)

Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, future=True)

# Public base URL (used for QR links)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")

# Role allowlists (comma-separated emails)
ADMIN_EMAILS       = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
INSTRUCTOR_EMAILS  = {e.strip().lower() for e in os.getenv("INSTRUCTOR_EMAILS", "").split(",") if e.strip()}
SECRETARY_EMAILS   = {e.strip().lower() for e in os.getenv("SECRETARY_EMAILS", "").split(",") if e.strip()}

# ---- Models ----
class User(Base):
    __tablename__ = "users"
    id          = Column(Integer, primary_key=True)
    name        = Column(String, nullable=False)
    email       = Column(String, nullable=False, unique=True)
    role        = Column(String, nullable=False)  # "admin" or "instructor"
    teaches     = relationship("CourseInstructor", back_populates="user")

class Course(Base):
    __tablename__ = "courses"
    id          = Column(Integer, primary_key=True)
    code        = Column(String, nullable=False, unique=True)
    title       = Column(String, nullable=False)
    instructors = relationship("CourseInstructor", back_populates="course")
    sessions    = relationship("Session", back_populates="course")

class CourseInstructor(Base):
    __tablename__ = "course_instructors"
    id        = Column(Integer, primary_key=True)
    course_id = Column(Integer, ForeignKey("courses.id"), nullable=False)
    user_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    course    = relationship("Course", back_populates="instructors")
    user      = relationship("User", back_populates="teaches")
    __table_args__ = (UniqueConstraint('course_id', 'user_id', name='_course_inst_uc'),)
    

class Session(Base):
    __tablename__ = "sessions"
    id         = Column(Integer, primary_key=True)
    course_id  = Column(Integer, ForeignKey("courses.id"), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time   = Column(DateTime, nullable=True)
    is_open    = Column(Boolean, default=True)
    token      = Column(String, nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)
    course     = relationship("Course", back_populates="sessions")
    attendance = relationship("Attendance", back_populates="session")

class Attendance(Base):
    __tablename__ = "attendance"
    id            = Column(Integer, primary_key=True)
    session_id    = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    student_name  = Column(String, nullable=False)
    student_email = Column(String, nullable=False)
    created_at    = Column(DateTime, nullable=False)
    session       = relationship("Session", back_populates="attendance")
    __table_args__ = (UniqueConstraint('session_id', 'student_email', name='_unique_sess_email'),)

# Create tables (ok for dev; for prod use migrations)
Base.metadata.create_all(engine)

# -----------------------------
# Helpers
# -----------------------------

def get_report_base_query(db, instructor_email=None, course_ids=None, date_from=None, date_to=None):
    q = (
        db.query(
            Course.code.label("course_code"),
            Course.title.label("course_title"),
            Session.id.label("session_id"),
            Session.start_time.label("session_start"),
            Attendance.student_name,
            Attendance.student_email,
            Attendance.created_at.label("check_in_at"),
        )
        .join(Session, Session.course_id == Course.id)
        .join(Attendance, Attendance.session_id == Session.id)
    )
    # Instructor scoping
    if instructor_email:
        q = q.join(CourseInstructor, CourseInstructor.course_id == Course.id)\
             .join(User, User.id == CourseInstructor.user_id)\
             .filter(User.email == instructor_email)
    # Filters
    if course_ids:
        q = q.filter(Course.id.in_(course_ids))
    if date_from:
        q = q.filter(Attendance.created_at >= date_from)
    if date_to:
        q = q.filter(Attendance.created_at < date_to)

    return q

def df_from_query(q):
    rows = q.all()
    if not rows:
        return pd.DataFrame(columns=[
            "course_code","course_title","session_id","session_start",
            "student_name","student_email","check_in_at"
        ])
    df = pd.DataFrame(rows, columns=[
        "course_code","course_title","session_id","session_start",
        "student_name","student_email","check_in_at"
    ])
    LOCAL_TZ = "Europe/Athens"
    df["session_start"] = pd.to_datetime(df["session_start"], utc=True).dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)
    df["check_in_at"]   = pd.to_datetime(df["check_in_at"],   utc=True).dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)
    return df

def group_df(df: pd.DataFrame, freq: str = "D"):
    """
    freq: "D" | "W-MON" | "MS"
    Returns (grouped, pivot)
    """
    if df.empty:
        return df, df

    # Ensure datetime
    ts = pd.to_datetime(df["check_in_at"], utc=False, errors="coerce")

    # Compute bucket start time safely for non-fixed freqs
    if freq == "D":
        bucket = ts.dt.floor("D")
    elif freq.startswith("W"):  # e.g., "W-MON"
        bucket = ts.dt.to_period(freq).dt.start_time
    elif freq in ("MS", "M"):
        # "MS" = month start; "M" = month end -> normalize to month start for consistency
        bucket = ts.dt.to_period("M").dt.start_time
    else:
        # Fallback: try floor; if it fails, default to day
        try:
            bucket = ts.dt.floor(freq)
        except Exception:
            bucket = ts.dt.floor("D")

    df = df.copy()
    df["bucket"] = bucket

    grouped = (
        df.groupby(["course_code", "course_title", "bucket"])
          .agg(
              check_ins=("student_email", "count"),
              unique_students=("student_email", "nunique"),
              sessions=("session_id", "nunique"),
          )
          .reset_index()
          .sort_values(["bucket", "course_code"])
    )

    pivot = (
        grouped.pivot_table(
            index="bucket",
            columns="course_code",
            values="check_ins",
            aggfunc="sum",
            fill_value=0,
        )
        .sort_index()
    )

    return grouped, pivot


def course_attendance_rates(df: pd.DataFrame):
    """
    Per course, per student: how many sessions attended vs total sessions of that course.
    """
    if df.empty:
        return pd.DataFrame()
    # total sessions per course
    total_sessions = df.drop_duplicates(["course_code","session_id"])\
                       .groupby("course_code")["session_id"].nunique().rename("total_sessions")
    # sessions attended per student per course
    attended = df.drop_duplicates(["course_code","session_id","student_email"])\
                 .groupby(["course_code","student_email"])["session_id"].nunique().rename("attended_sessions")\
                 .reset_index()
    out = attended.merge(total_sessions, on="course_code")
    out["attendance_rate_%"] = (out["attended_sessions"] / out["total_sessions"] * 100).round(1)
    return out.sort_values(["course_code","attendance_rate_%"], ascending=[True, False])


def to_aware_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def fmt_local(dt):
    if dt is None:
        return "-"
    return to_aware_utc(dt).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')

def get_db():
    return SessionLocal()

def gen_token():
    return uuid.uuid4().hex

def now_utc():
    return datetime.now(timezone.utc)

def qr_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def b64img(b: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(b).decode()

def ensure_demo_data(db):
    # Create a demo admin and instructor if not present
    if db.query(User).count() == 0:
        admin = User(name="Admin User", email="admin@example.com", role="admin")
        inst  = User(name="Instructor One", email="instructor@example.com", role="instructor")
        db.add_all([admin, inst])
        db.commit()
    if db.query(Course).count() == 0:
        c1 = Course(code="PTIA-101", title="Προηγμένες Τεχνολογίες Πληροφορικής")
        db.add(c1); db.commit()
        inst = db.query(User).filter_by(role="instructor").first()
        db.add(CourseInstructor(course_id=c1.id, user_id=inst.id))
        db.commit()

def instructor_courses(db, instructor_email):
    u = db.query(User).filter_by(email=instructor_email, role="instructor").first()
    if not u:
        return []
    links = db.query(CourseInstructor).filter_by(user_id=u.id).all()
    ids = [l.course_id for l in links]
    return db.query(Course).filter(Course.id.in_(ids)).all()

def current_user():
    """
    Read authenticated user from reverse proxy headers (oauth2-proxy/ingress).
    Falls back to optional env vars for local dev: DEV_FAKE_EMAIL / DEV_FAKE_NAME.
    Never touches st.secrets unless present, and only inside try/except.
    """
    email = (
        os.getenv("X_AUTH_REQUEST_EMAIL")
        or os.getenv("X-Auth-Request-Email")
        or os.getenv("HTTP_X_AUTH_REQUEST_EMAIL")  # some proxies prefix HTTP_
    )
    name = (
        os.getenv("X_AUTH_REQUEST_USER")
        or os.getenv("X-Auth-Request-User")
        or os.getenv("HTTP_X_AUTH_REQUEST_USER")
    )

    # Dev fallback (optional): allow running the app without the proxy
    if not email:
        email = os.getenv("DEV_FAKE_EMAIL")  # e.g., gfragi@hua.gr
    if not name:
        name = os.getenv("DEV_FAKE_NAME")    # e.g., George Fragiadakis

    # Optional: only touch st.secrets if it actually exists
    try:
        if not email and hasattr(st, "secrets") and "auth_email" in st.secrets:
            email = st.secrets["auth_email"]
        if not name and hasattr(st, "secrets") and "auth_name" in st.secrets:
            name = st.secrets.get("auth_name", None)
    except Exception:
        # No secrets file / parse error — ignore
        pass

    if email:
        email = email.strip().lower()
    return {"email": email, "name": name}

def require_domain(email: str, domain: str = EMAIL_DOMAIN) -> bool:
    return bool(email and email.endswith(domain))

def is_admin(email: str) -> bool:
    return email in ADMIN_EMAILS

def is_instructor(email: str) -> bool:
    return email in INSTRUCTOR_EMAILS or is_admin(email)

def is_secretary(email: str) -> bool:
    # Secretaires have report access but not user/course management (adjust as you like)
    return email in SECRETARY_EMAILS or is_admin(email)

# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title=APP_TITLE, page_icon="✅", layout="wide")
st.title(APP_TITLE)

u = current_user()
if not u["email"]:
    st.error("You are not authenticated. Please access the app through the university login (Google SSO).")
    st.stop()

st.caption(f"Signed in as **{u.get('name') or u['email']}**")

if not require_domain(u["email"]):
    st.error(f"Only accounts under **{EMAIL_DOMAIN}** are allowed.")
    st.stop()
tabs = st.tabs(["Student Check-in", "Instructor Panel", "Admin Panel", "Reports"])

# ----------------------------------
# Student public check-in (with token)
# ----------------------------------
with tabs[0]:
    st.subheader("Student Check-in")

    params = st.query_params
    session_token = params.get("session", None)
    if session_token is None:
        session_token = st.text_input("Session token (from QR link):", value=session_token or "")

    if st.button("Load Session") or session_token:
        db = get_db()
        sess = db.query(Session).filter_by(token=session_token).first()
        if not sess:
            st.error("Invalid session token.")
        else:
            if not sess.is_open:
                st.warning("This session is closed.")
            elif now_utc() > to_aware_utc(sess.expires_at):
                st.warning("This session has expired.")
            else:
                st.success(f"Course: {sess.course.title} — open until {fmt_local(sess.expires_at)}")

                # Prefer display name from SSO; allow override if missing
                default_name = u.get("name") or ""
                with st.form("checkin_form"):
                    student_name = st.text_input("Full name (Ονοματεπώνυμο)", value=default_name)
                    submit = st.form_submit_button("Submit Attendance")

                student_email = u["email"]  # from Google SSO
                if submit:
                    if not student_name.strip():
                        st.error("Please provide your full name.")
                    else:
                        exists = db.query(Attendance).filter_by(session_id=sess.id, student_email=student_email).first()
                        if exists:
                            st.info("You are already recorded for this session.")
                        else:
                            rec = Attendance(
                                session_id=sess.id,
                                student_name=student_name.strip(),
                                student_email=student_email,
                                created_at=now_utc(),
                            )
                            db.add(rec)
                            db.commit()
                            st.success("Attendance recorded. Thank you!")

# ----------------------------------
# Instructor Panel
# ----------------------------------
with tabs[1]:
    st.subheader("Instructor Panel")

    if not is_instructor(u["email"]):
        st.info("Instructor access only.")
        st.stop()

    instructor_email = u["email"]  # from SSO
    db = get_db()

    my_courses = instructor_courses(db, instructor_email)
    if not my_courses:
        st.warning("No courses assigned to your account. Contact the secretary.")
    else:
        colA, colB = st.columns([2, 1])
        with colA:
            course = st.selectbox(
                "Select course",
                options=my_courses,
                format_func=lambda c: f"{c.code} — {c.title}"
            )
        with colB:
            duration = st.number_input(
                "Session duration (minutes)",
                min_value=5, max_value=240, value=SESSION_DEFAULT_MINUTES
            )

        if st.button("Open new attendance session"):
            token = gen_token()
            new_sess = Session(
                course_id=course.id,
                start_time=now_utc(),
                is_open=True,
                token=token,
                expires_at=now_utc() + timedelta(minutes=int(duration)),
            )
            db.add(new_sess); db.commit()
            st.success("Session opened.")

        st.markdown("### Active Sessions")
        active = db.query(Session).filter_by(course_id=course.id, is_open=True)\
                 .order_by(Session.start_time.desc()).all()
        if not active:
            st.info("No active sessions.")
        else:
            for sess in active:
                st.write(f"**Started:** {fmt_local(sess.start_time)} | **Expires:** {fmt_local(sess.expires_at)}")
                public_url = f"{PUBLIC_BASE_URL}/?session={sess.token}"
                png = qr_bytes(public_url)
                st.image(png, caption="Scan to check-in")
                st.code(public_url, language="text")

                c1, c2, c3 = st.columns(3)
                with c1:
                    if st.button("Close session", key=f"close_{sess.id}"):
                        sess.is_open = False
                        sess.end_time = now_utc()
                        db.commit()
                        st.success("Session closed.")
                with c2:
                    if st.button("Extend 10 minutes", key=f"extend_{sess.id}"):
                        sess.expires_at = max(to_aware_utc(sess.expires_at), now_utc()) + timedelta(minutes=10)
                        db.commit()
                        st.success("Extended by 10 minutes.")
                with c3:
                    count = db.query(Attendance).filter_by(session_id=sess.id).count()
                    st.metric("Current check-ins", count)

        # Reports (same as you added), just reuse instructor_email
        st.markdown("### 📊 Instructor Reports")
        date_col1, date_col2, grp_col = st.columns([1,1,1])
        with date_col1:
            date_from = st.date_input("From date", value=pd.Timestamp.today().normalize() - pd.Timedelta(days=30), key="instructor_from")
        with date_col2:
            date_to = st.date_input("To date", value=pd.Timestamp.today().normalize() + pd.Timedelta(days=1), key="instructor_to")
        with grp_col:
            bucket = st.selectbox("Group by", ["Day (D)", "Week (W-MON)", "Month (MS)"], index=0, key="instructor_groupby")

        freq_map = {"Day (D)":"D", "Week (W-MON)":"W-MON", "Month (MS)":"MS"}
        freq = freq_map[bucket]

        course_choice = st.multiselect(
            "Courses",
            options=my_courses,
            format_func=lambda c: f"{c.code} — {c.title}",
            default=my_courses,
            key="instructor_courses"
        )
        course_ids_sel = [c.id for c in course_choice] or [c.id for c in my_courses]

        if st.button("Run report", key="instructor_run_report"):
            q = get_report_base_query(
                db,
                instructor_email=instructor_email,
                course_ids=course_ids_sel,
                date_from=pd.Timestamp(date_from).tz_localize("UTC"),
                date_to=pd.Timestamp(date_to).tz_localize("UTC"),
            )
            df = df_from_query(q)
            st.subheader("Raw check-ins (sortable)")
            st.dataframe(df.sort_values("check_in_at", ascending=False), use_container_width=True)
            st.download_button("Download CSV (raw)", df.to_csv(index=False).encode(), file_name="instructor_checkins.csv", mime="text/csv")

            st.subheader(f"Aggregates per {bucket.split()[0]} & course")
            grouped, pivot = group_df(df, freq=freq)
            st.dataframe(grouped, use_container_width=True)
            st.download_button("Download CSV (grouped)", grouped.to_csv(index=False).encode(), file_name="instructor_grouped.csv", mime="text/csv")

            st.subheader("Pivot (rows=time bucket, columns=course_code)")
            st.dataframe(pivot, use_container_width=True)
            st.download_button("Download CSV (pivot)", pivot.to_csv().encode(), file_name="instructor_pivot.csv", mime="text/csv")

            st.subheader("Per-student attendance rate (%) per course")
            rates = course_attendance_rates(df)
            st.dataframe(rates, use_container_width=True)
            st.download_button("Download CSV (rates)", rates.to_csv(index=False).encode(), file_name="instructor_rates.csv", mime="text/csv")
# ----------------------------------
# Admin Panel
# ----------------------------------
with tabs[2]:
    if not (is_admin(u["email"]) or is_secretary(u["email"])):
        st.subheader("Admin / Secretariat")
        st.info("Access restricted.")
        st.stop()

    st.subheader("Admin / Secretariat Panel")

    db = get_db()

    if is_admin(u["email"]):
        # Full management
        st.markdown("#### Users")
        with st.form("add_user_form"):
            name = st.text_input("Name")
            email = st.text_input("Email")
            role = st.selectbox("Role", ["admin", "instructor"])
            add_u = st.form_submit_button("Add user")
        if add_u:
            if not name or not email:
                st.error("Name and email required.")
            elif db.query(User).filter_by(email=email.lower().strip()).first():
                st.warning("User already exists.")
            else:
                db.add(User(name=name, email=email.lower().strip(), role=role)); db.commit()
                st.success("User added.")

        st.markdown("#### Courses")
        with st.form("add_course_form"):
            code = st.text_input("Course code")
            title = st.text_input("Course title")
            add_c = st.form_submit_button("Add course")
        if add_c:
            if not code or not title:
                st.error("Code and title required.")
            elif db.query(Course).filter_by(code=code).first():
                st.warning("Course already exists.")
            else:
                db.add(Course(code=code, title=title)); db.commit()
                st.success("Course added.")

        st.markdown("#### Assign Instructor to Course")
        users = db.query(User).filter_by(role="instructor").all()
        courses = db.query(Course).all()
        if users and courses:
            u_sel = st.selectbox("Instructor", users, format_func=lambda u_: f"{u_.name} ({u_.email})")
            c_sel = st.selectbox("Course", courses, format_func=lambda c: f"{c.code} — {c.title}")
            if st.button("Assign"):
                exists = db.query(CourseInstructor).filter_by(course_id=c_sel.id, user_id=u_sel.id).first()
                if exists:
                    st.info("Already assigned.")
                else:
                    db.add(CourseInstructor(course_id=c_sel.id, user_id=u_sel.id)); db.commit()
                    st.success("Assigned.")
        else:
            st.info("Add at least one instructor and one course.")
    else:
        st.info("Secretary mode: reports only (no user/course management).")
# ----------------------------------
# Reports (quick global view)
# ----------------------------------
with tabs[3]:
    if not (is_admin(u["email"]) or is_secretary(u["email"])):
        st.subheader("Admin Reports")
        st.info("Access restricted.")
        st.stop()

    st.subheader("Admin Reports")

    db = get_db()
    all_courses = db.query(Course).order_by(Course.code.asc()).all()

    c1, c2, c3 = st.columns([1,1,1])
    with c1:
        date_from = st.date_input(
    "From date",
    value=pd.Timestamp.today().normalize() - pd.Timedelta(days=30),
    key="admin_from"
)
    with c2:
        date_to = st.date_input(
    "To date",
    value=pd.Timestamp.today().normalize() + pd.Timedelta(days=1),
    key="admin_to"
)
    with c3:
        bucket = st.selectbox(
    "Group by",
    ["Day (D)", "Week (W-MON)", "Month (MS)"],
    index=0,
    key="admin_groupby"
)

    freq_map = {"Day (D)":"D", "Week (W-MON)":"W-MON", "Month (MS)":"MS"}
    freq = freq_map[bucket]

    course_choice = st.multiselect(
        "Courses",
        options=all_courses,
        format_func=lambda c: f"{c.code} — {c.title}",
        default=all_courses,
        key="admin_courses"
    )
    course_ids_sel = [c.id for c in course_choice] if course_choice else None

    if st.button("Run admin report"):
        q = get_report_base_query(
            db,
            instructor_email=None,  # admin sees all
            course_ids=course_ids_sel,
            date_from=pd.Timestamp(date_from).tz_localize("UTC"),
            date_to=pd.Timestamp(date_to).tz_localize("UTC"),
        )
        df = df_from_query(q)

        st.markdown("#### Raw")
        st.dataframe(df.sort_values(["course_code","check_in_at"]), use_container_width=True)
        st.download_button("Download CSV (raw)", df.to_csv(index=False).encode(),
                           file_name="admin_checkins_raw.csv", mime="text/csv")

        st.markdown("#### Aggregated per time bucket & course")
        grouped, pivot = group_df(df, freq=freq)
        st.dataframe(grouped, use_container_width=True)
        st.download_button("Download CSV (grouped)", grouped.to_csv(index=False).encode(),
                           file_name="admin_grouped.csv", mime="text/csv")

        st.markdown("#### Pivot")
        st.dataframe(pivot, use_container_width=True)
        st.download_button("Download CSV (pivot)", pivot.to_csv().encode(),
                           file_name="admin_pivot.csv", mime="text/csv")

        st.markdown("#### Per-student attendance rate by course")
        rates = course_attendance_rates(df)
        st.dataframe(rates, use_container_width=True)
        st.download_button("Download CSV (rates)", rates.to_csv(index=False).encode(),
                           file_name="admin_rates.csv", mime="text/csv")
