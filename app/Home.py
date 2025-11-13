import os
import io
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import qrcode
import base64

# =============================
# Initialize session state
# =============================
if 'authenticated_user' not in st.session_state:
    st.session_state.authenticated_user = None

# =============================
# Config
# =============================
APP_TITLE = "Centralized Attendance for University Courses"

EMAIL_DOMAIN = os.getenv("EMAIL_DOMAIN", "@hua.gr")
SESSION_DEFAULT_MINUTES = int(os.getenv("SESSION_DEFAULT_MINUTES", "15"))
OAUTH2_PREFIX = os.getenv("OAUTH2_PREFIX", "/oauth2").rstrip("/")
LOGOUT_URL = f"{OAUTH2_PREFIX}/sign_out"

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/attendance.db")
engine = create_engine(DATABASE_URL, echo=False, future=True)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine, future=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")

# Role allowlists
def parse_email_list(env_var_name, default=""):
    """Safely parse comma-separated email lists from environment variables"""
    raw_value = os.getenv(env_var_name, default)
    if not raw_value:
        return set()
    emails = {email.strip().lower() for email in raw_value.split(",") if email.strip()}
    return emails

ADMIN_EMAILS = parse_email_list("ADMIN_EMAILS", "gfragi@hua.gr")
INSTRUCTOR_EMAILS = parse_email_list("INSTRUCTOR_EMAILS", "gfragi@hua.gr")

AUTH_MODE = os.getenv("AUTH_MODE", "manual").strip().lower()
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").strip().lower() in ("true", "1", "yes", "on")

# =============================
# Models (keep your existing models)
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
# Helper Functions
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

def import_courses_and_instructors_from_df(df: pd.DataFrame) -> str:
    """Import courses and instructors from DataFrame with columns: course_code, course_title, instructor_name, instructor_email"""
    db = get_db()
    added_courses = 0
    added_instructors = 0
    added_assignments = 0
    
    for _, row in df.iterrows():
        course_code = row.get('course_code', '').strip()
        course_title = row.get('course_title', '').strip()
        instructor_name = row.get('instructor_name', '').strip()
        instructor_email = row.get('instructor_email', '').strip().lower()
        
        if not (course_code and course_title and instructor_name and instructor_email):
            continue
        
        # Add or get course
        course = db.query(Course).filter_by(code=course_code).first()
        if not course:
            course = Course(code=course_code, title=course_title)
            db.add(course)
            db.commit()
            added_courses += 1
        
        # Add or get instructor user
        user = db.query(User).filter_by(email=instructor_email).first()
        if not user:
            user = User(name=instructor_name, email=instructor_email, role="instructor")
            db.add(user)
            db.commit()
            added_instructors += 1
        
        # Assign instructor to course
        assignment = db.query(CourseInstructor).filter_by(course_id=course.id, user_id=user.id).first()
        if not assignment:
            db.add(CourseInstructor(course_id=course.id, user_id=user.id))
            db.commit()
            added_assignments += 1
    
    return f"✅ Import complete: {added_courses} courses, {added_instructors} instructors, {added_assignments} assignments added."

# =============================
# Auth / Roles - MUST BE DEFINED BEFORE USE
# =============================
def _qp_first(key: str) -> str | None:
    """Get first value from st.query_params for key."""
    q = st.query_params
    if key not in q:
        return None
    v = q.get(key)
    if isinstance(v, list):
        return (v[0] or "").strip()
    if isinstance(v, str):
        return v.strip()
    try:
        return str(v).strip() if v is not None else None
    except Exception:
        return None

def get_headers():
    """Get request headers using the new Streamlit method."""
    try:
        # NEW METHOD: Use st.context.headers
        if hasattr(st, 'context') and hasattr(st.context, 'headers'):
            return st.context.headers
    except Exception:
        pass
    
    # Fallback for older versions
    try:
        from streamlit.web.server.websocket_headers import _get_websocket_headers
        headers = _get_websocket_headers()
        if headers:
            return headers
    except ImportError:
        pass
    
    return {}

def email_to_display_name(email: str) -> str:
    """Convert email to a human-readable display name"""
    if not email:
        return "User"
    
    # Extract local part before @
    local_part = email.split('@')[0]
    
    # Common academic email patterns
    patterns = [
        ('.', ' '),    # john.doe -> John Doe
        ('_', ' '),    # john_doe -> John Doe  
        ('-', ' '),    # john-doe -> John Doe
    ]
    
    # Try each pattern
    for separator, replacement in patterns:
        if separator in local_part:
            parts = local_part.split(separator)
            # Capitalize each part and join with space
            return ' '.join(part.capitalize() for part in parts if part)
    
    # If no separators found, just capitalize the whole thing
    return local_part.capitalize()

def enhanced_current_user():
    """Enhanced user detection for LDAP environments with better name fallback"""
    if AUTH_MODE == "proxy":
        headers = get_headers()
        
        # Try ALL possible header names that might contain user info
        email = (
            headers.get("X-Auth-Request-Email") or
            headers.get("X-Email") or
            headers.get("X-Forwarded-Email") or
            headers.get("X-User-Email") or
            headers.get("X-LDAP-Email") or
            headers.get("X-REMOTE-USER") or  # Common in LDAP setups
            headers.get("REMOTE_USER") or    # Common in LDAP setups
            _qp_first("sso_email")
        )
        
        name = (
            headers.get("X-Auth-Request-User") or
            headers.get("X-User") or
            headers.get("X-Forwarded-User") or
            headers.get("X-LDAP-User") or
            headers.get("X-REMOTE-NAME") or
            headers.get("Display-Name") or
            headers.get("X-Full-Name") or  # Additional common header
            _qp_first("sso_name")
        )
        
        # ENHANCED: If we have email but no proper name, extract from email
        if email and (not name or name.isdigit() or '10877' in str(name)):
            name = email_to_display_name(email)
            
    else:
        email = _qp_first("email")
        name = _qp_first("name")
    
    email = (email or "").lower().strip() or None
    name = (name or "").strip() or None
    
    return {"email": email, "name": name}

def current_user():
    """Current user with session fallback"""
    u = enhanced_current_user()
    
    # If we have user info from headers, store it in session
    if u['email']:
        st.session_state.authenticated_user = u['email']
    
    # If no user info in headers but we have session, use session
    elif not u['email'] and st.session_state.authenticated_user:
        u = {"email": st.session_state.authenticated_user, "name": u.get('name') or "User"}
    
    return u

def is_admin(email: str) -> bool:
    """Check if email is in admin list (case-insensitive)"""
    if not email:
        return False
    return email.lower() in ADMIN_EMAILS

def is_instructor(email: str) -> bool:
    """Check if email is in instructor list or is admin (case-insensitive)"""
    if not email:
        return False
    return email.lower() in INSTRUCTOR_EMAILS or is_admin(email)

def debug_auth_comprehensive():
    """Comprehensive auth debugging - only shown when DEBUG_MODE=True"""
    if not DEBUG_MODE:
        return True  # Return True to continue with auth check
    
    st.sidebar.markdown("### 🔍 Auth Debug Info")
    
    # Get all headers
    headers = get_headers()
    st.sidebar.markdown("#### Headers Received:")
    for key, value in headers.items():
        if any(auth_key in key.lower() for auth_key in ['auth', 'user', 'email', 'x-']):
            st.sidebar.write(f"**{key}**: {value}")
    
    # Current user info
    u = current_user()
    st.sidebar.markdown("#### Current User:")
    st.sidebar.write(f"Email: `{u['email']}`")
    st.sidebar.write(f"Name: `{u['name']}`")
    
    # Query params
    st.sidebar.markdown("#### Query Parameters:")
    st.sidebar.write(dict(st.query_params))
    
    # Check if user is authenticated
    is_auth = bool(u['email'])
    st.sidebar.markdown("#### Authentication Status:")
    st.sidebar.write(f"Authenticated: **{'✅ YES' if is_auth else '❌ NO'}**")
    
    return is_auth

# =============================
# Page Setup - NOW SAFE TO CALL current_user()
# =============================
st.set_page_config(page_title=APP_TITLE, page_icon="✅", layout="wide")

# Get current user info - NOW THIS WILL WORK
u = current_user()
u_email = (u.get("email") or "").strip().lower()
u_name = (u.get("name") or "").strip()

def need_identity():
    return AUTH_MODE == "proxy" and not u_email

# Authentication gate
if need_identity():
    st.info("You need to sign in with your university account.")
    st.markdown(f'<a href="{OAUTH2_PREFIX}/start?rd=https://localhost:8443" target="_top">Continue to sign in</a>', unsafe_allow_html=True)
    st.stop()

# Call debug (will only show if debug mode is enabled)
debug_auth_comprehensive()

# =============================
# Role Debug Info - NOW u_email IS DEFINED
# =============================
if DEBUG_MODE:
    st.sidebar.markdown("### 👥 Role Debug Info")
    st.sidebar.write(f"Admin emails: {list(ADMIN_EMAILS)}")
    st.sidebar.write(f"Instructor emails: {list(INSTRUCTOR_EMAILS)}")
    st.sidebar.write(f"Your email: `{u_email}`")
    st.sidebar.write(f"Is admin: **{'✅ YES' if is_admin(u_email) else '❌ NO'}**")
    st.sidebar.write(f"Is instructor: **{'✅ YES' if is_instructor(u_email) else '❌ NO'}**")

    # Debug current role access
    st.sidebar.markdown("### 🔧 Current Access")
    st.sidebar.write(f"Can see Instructor Panel: {is_instructor(u_email)}")
    st.sidebar.write(f"Can see Admin Panel: {is_admin(u_email)}")
    st.sidebar.write(f"Can see Reports: {is_admin(u_email)}")


# =============================

# Logo and header (keep your existing code)
@st.cache_data
def _b64_or_empty(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return ""

LOGO_PATH = Path(__file__).parent / "assets" / "dit_hua_logo.png"
LOGO_DATA_B64 = _b64_or_empty(str(LOGO_PATH))
LOGO_DATA_URL = f"data:image/png;base64,{LOGO_DATA_B64}" if LOGO_DATA_B64 else ""

right_block = ""
if AUTH_MODE == "proxy" and u_email:
    right_block = (
        f"""<div class="hua-right">
              Signed in as <strong>{u_email}</strong>
              &nbsp; | &nbsp; <a href="{LOGOUT_URL}" target="_top">Logout</a>
            </div>"""
    )

st.markdown(
    f"""
    <style>
      .hua-header {{
        display:flex; align-items:center; gap:18px;
        border-bottom:1px solid var(--secondary-background-color);
        padding:10px 8px 12px 8px; margin-bottom:6px;
      }}
      .hua-left {{ display:flex; align-items:center; gap:16px; min-width:0; }}
      .hua-logo img {{ height:52px; width:auto; display:block; }}
      .hua-title {{ line-height:1.15; }}
      .hua-title .line1 {{ font-size:22px; font-weight:700; margin:0; white-space:nowrap; }}
      .hua-right {{ margin-left:auto; text-align:right; font-size:15px; }}
      .hua-right a {{ color:#0b6efd; text-decoration:none; }}
      .hua-right a:hover {{ text-decoration:underline; }}
      @media (max-width:680px) {{
        .hua-title .line1 {{ font-size:18px; }}
        .hua-logo img {{ height:44px; }}
      }}
      @media (max-width:520px) {{
        .hua-header {{ flex-wrap:wrap; gap:10px; }}
        .hua-right {{ width:100%; text-align:left; }}
      }}
    </style>
    <div class="hua-header">
      <div class="hua-left">
        <a class="hua-logo" href="https://dit.hua.gr/" target="_blank" rel="noopener">
          {"<img src='" + LOGO_DATA_URL + "' alt='Harokopio University - Dept. of Informatics & Telematics'/>" if LOGO_DATA_URL else ""}
        </a>
        <div class="hua-title">
          <p class="line1">{APP_TITLE}</p>
        </div>
      </div>
      {right_block}
    </div>
    """,
    unsafe_allow_html=True,
)

# Manual auth (keep your existing code)
if AUTH_MODE == "manual":
    with st.expander("Manual sign-in (admins/instructors)"):
        man_name  = st.text_input("Your name", value=u_name or "")
        man_email = st.text_input("Your academic email", value=u_email or "",
                                  placeholder=f"name.surname{EMAIL_DOMAIN}")
        use_it = st.button("Use this identity")
        if use_it:
            st.query_params["name"] = man_name.strip()
            st.query_params["email"] = man_email.strip().lower()
            st.rerun()


# =============================
# Tabs
# =============================

labels = ["Student Check-in"]
if is_instructor(u_email):
    labels.append("Instructor Panel")
if is_admin(u_email):
    labels += ["Admin Panel", "Reports"]
labels.append("Help")
tabs = st.tabs(labels)
tab_index = {name: i for i, name in enumerate(labels)}

# ----------------------------------
# Student public check-in
# ----------------------------------
with tabs[tab_index["Student Check-in"]]:
    st.subheader("Student Check-in")

    params = st.query_params
    session_token = params.get("session", None)
    autocheck = str(params.get("autocheckin", "")).lower() in {"1", "true", "yes"}

    def do_autocheckin(db, sess, email_from_sso: str, name_from_sso: str | None):
        def _derive_name(email_: str) -> str:
            local = email_.split("@", 1)[0].replace(".", " ").replace("_", " ").strip()
            return " ".join(w.capitalize() for w in local.split())

        student_email = email_from_sso.strip().lower()
        student_name  = (name_from_sso or _derive_name(student_email)).strip()

        exists = db.query(Attendance).filter_by(session_id=sess.id, student_email=student_email).first()
        if exists:
            st.info("You are already recorded for this session.")
            return

        rec = Attendance(session_id=sess.id, student_name=student_name,
                         student_email=student_email, created_at=now_utc())
        db.add(rec); db.commit()
        st.success("✅ Attendance recorded. Thank you!")

    if not session_token:
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

                sso_email = u_email
                sso_name  = u_name or None

                if autocheck:
                    if not sso_email:
                        st.error("Authentication is required to auto check-in. Please sign in and retry.")
                    else:
                        do_autocheckin(db, sess, sso_email, sso_name)
                        st.stop()

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
                            rec = Attendance(session_id=sess.id,
                                             student_name=" ".join(student_name.split()),
                                             student_email=final_email,
                                             created_at=now_utc())
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
                course = st.selectbox("Select course", options=my_courses,
                                      format_func=lambda c: f"{c.code} — {c.title}")
            with colB:
                duration = st.number_input("Session duration (minutes)",
                                           min_value=5, max_value=240, value=SESSION_DEFAULT_MINUTES,
                                           help="How long the QR/link accepts check-ins.")

            if st.button("Open new attendance session", help="Creates a timed session and QR/URL for students to scan."):
                token = gen_token()
                new_sess = Session(course_id=course.id, start_time=now_utc(),
                                   is_open=True, token=token,
                                   expires_at=now_utc() + timedelta(minutes=int(duration)))
                db.add(new_sess); db.commit()
                st.success("Session opened.")

            st.markdown("### Active Sessions")
            active = db.query(Session).filter_by(course_id=course.id, is_open=True)\
                    .order_by(Session.start_time.desc()).all()
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

            course_choice = st.multiselect("Courses", options=my_courses,
                                           format_func=lambda c: f"{c.code} — {c.title}",
                                           default=my_courses, key="instructor_courses")
            course_ids_sel = [c.id for c in course_choice] or [c.id for c in my_courses]

            if st.button("Run report", key="instructor_run_report"):
                q = get_report_base_query(db, instructor_email=u_email, course_ids=course_ids_sel,
                                          date_from=pd.Timestamp(date_from).tz_localize("UTC"),
                                          date_to=pd.Timestamp(date_to).tz_localize("UTC"))
                df = df_from_query(q)
                st.subheader("Raw check-ins (sortable)")
                st.dataframe(df.sort_values("check_in_at", ascending=False), width='stretch')
                st.download_button("Download CSV (raw)", df.to_csv(index=False).encode(),
                                   file_name="instructor_checkins.csv", mime="text/csv")

                st.subheader(f"Aggregates per {bucket.split()[0]} & course")
                grouped, pivot = group_df(df, freq=freq)
                st.dataframe(grouped, width='stretch')
                st.download_button("Download CSV (grouped)", grouped.to_csv(index=False).encode(),
                                   file_name="instructor_grouped.csv", mime="text/csv")

                st.subheader("Pivot (rows=time bucket, columns=course_code)")
                st.dataframe(pivot, width='stretch')
                st.download_button("Download CSV (pivot)", pivot.to_csv().encode(),
                                   file_name="instructor_pivot.csv", mime="text/csv")

                st.subheader("Per-student attendance rate (%) per course")
                rates = course_attendance_rates(df)
                st.dataframe(rates, width='stretch')
                st.download_button("Download CSV (rates)", rates.to_csv(index=False).encode(),
                                   file_name="instructor_rates.csv", mime="text/csv")

# --- Admin Panel ---
if "Admin Panel" in tab_index:
    with tabs[tab_index["Admin Panel"]]:
        st.subheader("Admin / Secretariat")
        if not (is_admin(u_email)):
            st.info("Access restricted.")
            st.stop()

        db = get_db()

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
            
            st.markdown("---")
        st.markdown("#### Bulk Import Courses & Instructors from CSV")

        uploaded_file = st.file_uploader("Upload CSV file", type=['csv'])
        if uploaded_file:
            df = pd.read_csv(uploaded_file)
            st.dataframe(df)
            
            if st.button("Import Data"):
                result = import_courses_and_instructors_from_df(df)
                st.success(result)

        # Or paste data directly
        st.markdown("#### Or paste data directly")
        pasted_data = st.text_area("Paste tab-separated data (Course Code, Course Title, Instructor Name, Instructor Email)", height=200)
        if pasted_data and st.button("Import from text"):
            # Parse pasted data
            lines = pasted_data.strip().split('\n')
            data = []
            for line in lines:
                parts = line.split('\t')
                if len(parts) >= 4:
                    data.append({
                        'course_code': parts[0],
                        'course_title': parts[1], 
                        'instructor_name': parts[2],
                        'instructor_email': parts[3]
                    })
            
            df = pd.DataFrame(data)
            result = import_courses_and_instructors_from_df(df)
            st.success(result)


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

        course_choice = st.multiselect("Courses", options=all_courses,
                                       format_func=lambda c: f"{c.code} — {c.title}",
                                       default=all_courses, key="admin_courses")
        course_ids_sel = [c.id for c in course_choice] if course_choice else None

        if st.button("Run admin report"):
            q = get_report_base_query(db, instructor_email=None, course_ids=course_ids_sel,
                                      date_from=pd.Timestamp(date_from).tz_localize("UTC"),
                                      date_to=pd.Timestamp(date_to).tz_localize("UTC"))
            df = df_from_query(q)

            st.markdown("#### Raw")
            st.dataframe(df.sort_values(["course_code","check_in_at"]), width='stretch')
            st.download_button("Download CSV (raw)", df.to_csv(index=False).encode(),
                               file_name="admin_checkins_raw.csv", mime="text/csv")

            st.markdown("#### Aggregated per time bucket & course")
            grouped, pivot = group_df(df, freq=freq)
            st.dataframe(grouped, width='stretch')
            st.download_button("Download CSV (grouped)", grouped.to_csv(index=False).encode(),
                               file_name="admin_grouped.csv", mime="text/csv")

            st.markdown("#### Pivot")
            st.dataframe(pivot, width='stretch')
            st.download_button("Download CSV (pivot)", pivot.to_csv().encode(),
                               file_name="admin_pivot.csv", mime="text/csv")

            st.markdown("#### Per-student attendance rate by course")
            rates = course_attendance_rates(df)
            st.dataframe(rates, width='stretch')
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
- If the page keeps "loading", the proxy must allow **WebSocket** upgrade headers.  
- If you see "Not authenticated", confirm `REQUIRE_SSO=true` and OAuth2 Proxy is in front.
""")


st.markdown("----")
st.markdown("© Harokopio University of Athens - Dept. of Informatics & Telematics | Developed by DIT | 2025")