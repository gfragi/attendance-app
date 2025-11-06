import os
import io
import uuid
import base64
from pathlib import Path
from datetime import datetime, timedelta, timezone
from streamlit.components.v1 import html as st_html

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import qrcode

# =============================
# Config
# =============================
APP_TITLE = "Centralized Attendance for University Courses"

EMAIL_DOMAIN = os.getenv("EMAIL_DOMAIN", "@hua.gr")
SESSION_DEFAULT_MINUTES = int(os.getenv("SESSION_DEFAULT_MINUTES", "15"))
OAUTH2_PREFIX = os.getenv("OAUTH2_PREFIX", "/oauth2").rstrip("/")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/attendance.db")
engine = create_engine(DATABASE_URL, echo=False, future=True)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, future=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")

# Role allowlists (comma-separated emails)
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
INSTRUCTOR_EMAILS = {e.strip().lower() for e in os.getenv("INSTRUCTOR_EMAILS", "").split(",") if e.strip()}

# =============================
# Models
# =============================
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

Base.metadata.create_all(engine)

# =============================
# Helpers
# =============================
def get_db():
    return SessionLocal()

def now_utc():
    return datetime.now(timezone.utc)

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

def gen_token():
    return uuid.uuid4().hex

def qr_bytes(url: str) -> bytes:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def instructor_courses(db, instructor_email):
    u = db.query(User).filter_by(email=instructor_email, role="instructor").first()
    if not u:
        return []
    links = db.query(CourseInstructor).filter_by(user_id=u.id).all()
    ids = [l.course_id for l in links]
    if not ids:
        return []
    return db.query(Course).filter(Course.id.in_(ids)).all()

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
    if instructor_email:
        q = q.join(CourseInstructor, CourseInstructor.course_id == Course.id)\
             .join(User, User.id == CourseInstructor.user_id)\
             .filter(User.email == instructor_email)
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
    if df.empty:
        return df, df
    ts = pd.to_datetime(df["check_in_at"], utc=False, errors="coerce")
    if freq == "D":
        bucket = ts.dt.floor("D")
    elif freq.startswith("W"):
        bucket = ts.dt.to_period(freq).dt.start_time
    elif freq in ("MS","M"):
        bucket = ts.dt.to_period("M").dt.start_time
    else:
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
    if df.empty:
        return pd.DataFrame()
    total_sessions = df.drop_duplicates(["course_code","session_id"])\
                       .groupby("course_code")["session_id"].nunique().rename("total_sessions")
    attended = df.drop_duplicates(["course_code","session_id","student_email"])\
                 .groupby(["course_code","student_email"])["session_id"].nunique().rename("attended_sessions")\
                 .reset_index()
    out = attended.merge(total_sessions, on="course_code")
    out["attendance_rate_%"] = (out["attended_sessions"] / out["total_sessions"] * 100).round(1)
    return out.sort_values(["course_code","attendance_rate_%"], ascending=[True, False])

# =============================
# Auth / Roles
# =============================
def current_user():
    # SSO bridge writes sso_email/sso_name to URL query
    params = st.query_params
    email = params.get("sso_email")
    name  = params.get("sso_name")
    email = email.strip().lower() if isinstance(email, str) else None
    name  = name.strip() if isinstance(name, str) else None
    return {"email": email, "name": name}

def is_admin(email: str) -> bool:
    return bool(email) and email in ADMIN_EMAILS

def is_instructor(email: str) -> bool:
    return bool(email) and (email in INSTRUCTOR_EMAILS or is_admin(email))

# =============================
# Page chrome (logo + logout) and SSO bootstrap
# =============================
st.set_page_config(page_title=APP_TITLE, page_icon="✅", layout="wide")

@st.cache_data
def _b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

LOGO_PATH = Path(__file__).parent / "assets/HUA-Logo-Informatics-Telematics-EN-30-Years-RGB.png"
LOGO_DATA = f"data:image/png;base64,{_b64(str(LOGO_PATH))}"

u = current_user()
DEPT_URL   = "https://dit.hua.gr/"
LOGOUT_URL = "/oauth2/sign_out"
user_email = u.get("email") or ""

right_block = (
    f"""<div class="hua-right">
          Signed in as <strong>{user_email}</strong>
          &nbsp; | &nbsp; <a href="{LOGOUT_URL}" target="_top">Logout</a>

        </div>"""
    if user_email else ""
)

st.markdown(
    f"""
    <style>
      .hua-header {{ display:flex; align-items:center; gap:18px;
                     border-bottom:1px solid var(--secondary-background-color);
                     padding:10px 8px 12px 8px; margin-bottom:6px; }}
      .hua-left {{ display:flex; align-items:center; gap:16px; min-width:0; }}
      .hua-logo img {{ height:52px; width:auto; display:block; }}
      .hua-title {{ line-height:1.15; }}
      .hua-title .line1, .hua-title .line2 {{ font-size:22px; font-weight:700; margin:0; white-space:nowrap; }}
      .hua-right {{ margin-left:auto; text-align:right; font-size:15px; }}
      .hua-right a {{ color:#0b6efd; text-decoration:none; }}
      .hua-right a:hover {{ text-decoration:underline; }}
      @media (max-width:680px) {{ .hua-title .line1, .hua-title .line2 {{ font-size:18px; }} .hua-logo img {{ height:44px; }} }}
      @media (max-width:520px) {{ .hua-header {{ flex-wrap:wrap; gap:10px; }} .hua-right {{ width:100%; text-align:left; }} }}
    </style>
    <div class="hua-header">
      <div class="hua-left">
        <a class="hua-logo" href="https://dit.hua.gr/" target="_blank" rel="noopener">
          <img src="{LOGO_DATA}" alt="Harokopio University - Dept. of Informatics & Telematics"/>
        </a>
        <div class="hua-title">
          <p class="line1">Centralized Attendance for</p>
          <p class="line2">University Courses</p>
        </div>
      </div>
      {right_block}
    </div>
    """,
    unsafe_allow_html=True,
)
# =============================
# Main logic
# =============================
REQUIRE_SSO = os.getenv("REQUIRE_SSO", "false").strip().lower() == "true"



def need_sso_claims() -> bool:
    p = st.query_params
    return not (isinstance(p.get("sso_email"), str) and p.get("sso_email"))

if REQUIRE_SSO and need_sso_claims():
    st_html(
        f"""
        <script>
        (async () => {{
          try {{
            const topWin = window.top || window;
            const here   = new URL(topWin.location.href);

            const res = await fetch('{OAUTH2_PREFIX}/userinfo', {{ credentials: 'include' }});
            if (!res.ok) {{
              topWin.location.href = '{OAUTH2_PREFIX}/start?rd=' + encodeURIComponent(here.toString());
              return;
            }}

            const data = await res.json();
            if (data && data.email) here.searchParams.set('sso_email', String(data.email).toLowerCase());
            if (data && (data.name || data.user)) here.searchParams.set('sso_name', String(data.name || data.user));

            topWin.location.replace(here.toString());
          }} catch (err) {{
            const u = new URL((window.top||window).location.href);
            (window.top||window).location.href = '{OAUTH2_PREFIX}/start?rd=' + encodeURIComponent(u.toString());
          }}
        }})();
        </script>
        """,
        height=1,
    )
    st.stop()

u = current_user()
u_email = (u.get("email") or "").strip().lower()
u_name  = (u.get("name")  or "").strip()

# =============================
# Tabs
# =============================

# ---------- Build tab list based on role ----------
labels = ["Student Check-in"]
if is_instructor(u_email):
    labels.append("Instructor Panel")
if is_admin(u_email):
    labels += ["Admin Panel", "Reports"]
labels.append("Help")

tabs = st.tabs(labels)

# We’ll keep an index map for clarity
tab_index = {name: i for i, name in enumerate(labels)}

# ----------------------------------
# Student public check-in (with token + optional auto-check)
# ----------------------------------
with tabs[tab_index["Student Check-in"]]:
    st.subheader("Student Check-in")

    params = st.query_params
    session_token = params.get("session", None)
    autocheck = str(params.get("autocheckin", "")).lower() in {"1", "true", "yes"}

    # Helper: do a server-side auto check-in (no form)
    def do_autocheckin(db, sess, email_from_sso: str, name_from_sso: str | None):
        # derive a display name if SSO didn't provide one
        def _derive_name(email_: str) -> str:
            local = email_.split("@", 1)[0]
            local = local.replace(".", " ").replace("_", " ").strip()
            return " ".join(w.capitalize() for w in local.split())

        student_email = email_from_sso.strip().lower()
        student_name  = (name_from_sso or _derive_name(student_email)).strip()

        exists = db.query(Attendance).filter_by(
            session_id=sess.id, student_email=student_email
        ).first()
        if exists:
            st.info("You are already recorded for this session.")
            return

        rec = Attendance(
            session_id=sess.id,
            student_name=student_name,
            student_email=student_email,
            created_at=now_utc(),
        )
        db.add(rec); db.commit()
        st.success("✅ Attendance recorded. Thank you!")

    if not session_token:
        # Manual entry path (kept for desktop or when someone types the URL)
        session_token = st.text_input("Session token (from QR link):", value=session_token or "")

    # When either user pressed "Load Session" or we already have a token from URL
    if st.button("Load Session") or session_token:
        db = get_db()
        sess = db.query(Session).filter_by(token=session_token).first()
        if not sess:
            st.error("Invalid session token.")
        else:
            # Basic session checks
            if not sess.is_open:
                st.warning("This session is closed.")
            elif now_utc() > to_aware_utc(sess.expires_at):
                st.warning("This session has expired.")
            else:
                st.success(f"Course: {sess.course.title} — open until {fmt_local(sess.expires_at)}")

                sso_email = (u.get("email") or "").strip().lower()
                sso_name  = (u.get("name") or "").strip() or None

                if autocheck:
                    # Auto mode: require SSO email; if missing, SSO bootstrap earlier will handle it
                    if not sso_email:
                        st.error("Authentication is required to auto check-in. Please sign in and retry.")
                    else:
                        do_autocheckin(db, sess, sso_email, sso_name)
                        # In auto mode we stop after recording to avoid re-submission on reruns
                        st.stop()

                # Fallback to form (desktop/manual)
                default_name = sso_name or ""
                with st.form("checkin_form"):
                    student_name = st.text_input("Full name (Ονοματεπώνυμο)", value=default_name)
                    if sso_email:
                        st.text_input("Academic email", value=sso_email, disabled=True)
                        student_email = sso_email
                    else:
                        student_email = st.text_input("Academic email", placeholder=f"name.surname{EMAIL_DOMAIN}")
                        st.caption(f"Only emails under **{EMAIL_DOMAIN}** are accepted.")
                    submit = st.form_submit_button("Submit Attendance")

                if submit:
                    if not student_name.strip():
                        st.error("Please provide your full name.")
                    elif not sso_email and not (student_email and student_email.endswith(EMAIL_DOMAIN)):
                        st.error(f"Email must be a valid **{EMAIL_DOMAIN}** address.")
                    else:
                        final_email = (student_email or sso_email).strip().lower()
                        exists = db.query(Attendance).filter_by(session_id=sess.id, student_email=final_email).first()
                        if exists:
                            st.info("You are already recorded for this session.")
                        else:
                            rec = Attendance(
                                session_id=sess.id,
                                student_name=" ".join(student_name.split()),
                                student_email=final_email,
                                created_at=now_utc(),
                            )
                            db.add(rec); db.commit()
                            st.success("Attendance recorded. Thank you!")

# --- Instructor Panel ---
if "Instructor Panel" in tab_index:
    with tabs[tab_index["Instructor Panel"]]:
        st.subheader("Instructor Panel")
        if not is_instructor(u_email):
            st.info("Instructor access only.")
            st.stop()

        db = get_db()
        my_courses = instructor_courses(db, u_email)
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
                    min_value=5, max_value=240, value=SESSION_DEFAULT_MINUTES,
                    help="How long the QR/link accepts check-ins."
                )

            if st.button("Open new attendance session", help="Creates a timed session and QR/URL for students to scan."):
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
            # Hide already-expired sessions
            now = now_utc()
            active = [s for s in active if s.is_open and to_aware_utc(s.expires_at) > now]

            if not active:
                st.info("No active sessions.")
            else:
                for sess in active:
                    st.write(f"**Started:** {fmt_local(sess.start_time)} | **Expires:** {fmt_local(sess.expires_at)}")
                    public_url = f"{PUBLIC_BASE_URL}/?session={sess.token}&autocheckin=1"
                    st.image(qr_bytes(public_url), caption="Scan to check-in")
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

            # Reports
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
                    instructor_email=u_email,
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

# --- Admin Panel ---
if "Admin Panel" in tab_index:
    with tabs[tab_index["Admin Panel"]]:
        st.subheader("Admin / Secretariat")
        if not (is_admin(u_email)):
            st.info("Access restricted.")
            st.stop()

        db = get_db()

        if is_admin(u_email):
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
            st.info("Secretary mode: reporting access only (no user/course management).")

# --- Admin Reports (Admin) ---
if "Reports" in tab_index:
    with tabs[tab_index["Reports"]]:
        st.subheader("Admin Reports")
        if not (is_admin(u_email)):
            st.info("Access restricted.")
            st.stop()

        db = get_db()
        all_courses = db.query(Course).order_by(Course.code.asc()).all()

        c1, c2, c3 = st.columns([1,1,1])
        with c1:
            date_from = st.date_input("From date", value=pd.Timestamp.today().normalize() - pd.Timedelta(days=30), key="admin_from")
        with c2:
            date_to = st.date_input("To date", value=pd.Timestamp.today().normalize() + pd.Timedelta(days=1), key="admin_to")
        with c3:
            bucket = st.selectbox("Group by", ["Day (D)", "Week (W-MON)", "Month (MS)"], index=0, key="admin_groupby")

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
                instructor_email=None,
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

# --- Help ---
with tabs[tab_index["Help"]]:
    st.header("Help & Quick Start")

    st.subheader("🎓 Instructors — 60-second checklist")
    st.markdown("""
1. **Open session** → *Instructor Panel* → Select course → **Open new attendance session**  
2. **Show the QR** or share the URL.  
3. Watch **Current check-ins**; **Extend 10'** if needed.  
4. **Close session** when done.  
5. Export **CSV** from reports.
""")

    st.subheader("📑 Secretariat — reporting")
    st.markdown("""
- Open **Reports** → set **From/To** and **Group by** (Day/Week/Month).  
- Filter by **Courses**, then **Run admin report**.  
- Download **Raw**, **Grouped**, **Pivot**, **Rates** as CSV.
""")

    st.subheader("🛠️ Admin — setup")
    st.markdown("""
- **Add users** (admin/instructor).  
- **Add courses** and **Assign instructors**.  
- Instructors then see their courses in *Instructor Panel*.
""")

    st.subheader("❓ FAQ / Tips")
    st.markdown(f"""
- **Students must use `{EMAIL_DOMAIN}`**.  
- If a QR link expires, click **Extend 10'**.  
- All times are **Europe/Athens**.  
- If the page keeps “loading”, the proxy must allow **WebSocket** upgrade headers.  
- If you see “Not authenticated”, confirm `REQUIRE_SSO=true` and OAuth2 Proxy is in front.
""")
