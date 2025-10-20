import os
import io
import uuid
import base64
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Boolean, ForeignKey, UniqueConstraint
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import qrcode

# -----------------------------
# Config
# -----------------------------
APP_TITLE = "Centralized Attendance (Streamlit)"
EMAIL_DOMAIN = "@hua"   # set to "@hua.gr" or your exact domain rule
SESSION_DEFAULT_MINUTES = 15

# Switch to PostgreSQL by setting DATABASE_URL env var, e.g.:
# export DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///attendance.db")

# -----------------------------
# DB setup
# -----------------------------
engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

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
    id          = Column(Integer, primary_key=True)
    course_id   = Column(Integer, ForeignKey("courses.id"), nullable=False)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    course      = relationship("Course", back_populates="instructors")
    user        = relationship("User", back_populates="teaches")
    __table_args__ = (UniqueConstraint('course_id', 'user_id', name='_course_inst_uc'),)

class Session(Base):
    __tablename__ = "sessions"
    id          = Column(Integer, primary_key=True)
    course_id   = Column(Integer, ForeignKey("courses.id"), nullable=False)
    start_time  = Column(DateTime, nullable=False)
    end_time    = Column(DateTime, nullable=True)
    is_open     = Column(Boolean, default=True)
    token       = Column(String, nullable=False, unique=True)
    expires_at  = Column(DateTime, nullable=False)
    course      = relationship("Course", back_populates="sessions")
    attendance  = relationship("Attendance", back_populates="session")

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

# -----------------------------
# Helpers
# -----------------------------
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

# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title=APP_TITLE, page_icon="✅", layout="wide")
st.title(APP_TITLE)

tabs = st.tabs(["Student Check-in", "Instructor Panel", "Admin Panel", "Reports"])

# ----------------------------------
# Student public check-in (with token)
# ----------------------------------
with tabs[0]:
    st.subheader("Student Check-in")
    params = st.query_params  # dict-like
    session_token = params.get("session", None)
    if session_token is None:
        session_token = st.text_input("Session token (from QR link):", value=session_token or "")

    if st.button("Load Session") or session_token:
        db = get_db()
        sess = db.query(Session).filter_by(token=session_token).first()
        if not sess:
            st.error("Invalid session token.")
        else:
            # Validate session
            if not sess.is_open:
                st.warning("This session is closed.")
            elif now_utc() > to_aware_utc(sess.expires_at):
                st.warning("This session has expired.")
            else:
                st.success(f"Course: {sess.course.title} — open until {fmt_local(sess.expires_at)}")
                with st.form("checkin_form"):
                    student_name = st.text_input("Full name (Ονοματεπώνυμο)")
                    student_email = st.text_input(f"Academic email (must end with {EMAIL_DOMAIN})")
                    submit = st.form_submit_button("Submit Attendance")
                if submit:
                    if not student_name.strip():
                        st.error("Please provide your full name.")
                    elif EMAIL_DOMAIN not in student_email:
                        st.error(f"Email must contain '{EMAIL_DOMAIN}'.")
                    else:
                        # Prevent duplicates
                        exists = db.query(Attendance).filter_by(session_id=sess.id, student_email=student_email).first()
                        if exists:
                            st.info("You are already recorded for this session.")
                        else:
                            rec = Attendance(
                                session_id=sess.id,
                                student_name=student_name.strip(),
                                student_email=student_email.strip().lower(),
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
    st.caption("For demo purposes we use a simple passcode. Replace with your SSO/OAuth later.")
    instructor_email = st.text_input("Instructor email", value="instructor@example.com")
    instructor_pass = st.text_input("Instructor passcode", type="password")

    if st.button("Sign in (demo)"):
        st.session_state["instructor_ok"] = True

    if st.session_state.get("instructor_ok"):
        db = get_db()
        ensure_demo_data(db)
        my_courses = instructor_courses(db, instructor_email)
        if not my_courses:
            st.warning("No courses assigned to this instructor.")
        else:
            colA, colB = st.columns([2, 1])
            with colA:
                course = st.selectbox(
                    "Select course",
                    options=my_courses,
                    format_func=lambda c: f"{c.code} — {c.title}"
                )
            with colB:
                duration = st.number_input("Session duration (minutes)", min_value=5, max_value=240, value=SESSION_DEFAULT_MINUTES)

            # Create / Open new session
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

            # List sessions
            st.markdown("### Active Sessions")
            active = db.query(Session).filter_by(course_id=course.id, is_open=True).order_by(Session.start_time.desc()).all()
            if not active:
                st.info("No active sessions.")
            else:
                for sess in active:
                    st.write(f"**Started:** {fmt_local(sess.start_time)} | **Expires:** {fmt_local(sess.expires_at)}")

                    # In Streamlit, use st.experimental_get_query_params only for reading; construct URL manually:
                    BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8501")
                    public_url = f"{BASE_URL}/?session={sess.token}"

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
                            sess.expires_at = max(sess.expires_at, now_utc()) + timedelta(minutes=10)
                            db.commit()
                            st.success("Extended by 10 minutes.")
                    with c3:
                        count = db.query(Attendance).filter_by(session_id=sess.id).count()
                        st.metric("Current check-ins", count)

            # Past sessions + export
            st.markdown("### Past Sessions & Export")
            past = db.query(Session).filter(Session.course_id == course.id, Session.is_open == False).order_by(Session.start_time.desc()).all()
            if past:
                for sess in past:
                    st.write(f"**Session:** {fmt_local(sess.start_time)} – {'Closed ' + fmt_local(sess.end_time) if sess.end_time else 'Closed'}")
                    rows = db.query(Attendance).filter_by(session_id=sess.id).all()
                    df = pd.DataFrame([{
                        "created_at": fmt_local(r.created_at),
                        "student_name": r.student_name,
                        "student_email": r.student_email
                    } for r in rows])
                    st.dataframe(df if not df.empty else pd.DataFrame(columns=["created_at", "student_name", "student_email"]))
                    if not df.empty:
                        csv = df.to_csv(index=False).encode()
                        st.download_button("Download CSV", data=csv, file_name=f"attendance_{course.code}_{sess.id}.csv", mime="text/csv")
            else:
                st.info("No past sessions yet.")

# ----------------------------------
# Admin Panel
# ----------------------------------
with tabs[2]:
    st.subheader("Admin Panel")
    st.caption("Demo admin (no real auth). Replace with your IdP/SSO later.")
    admin_ok = st.checkbox("I am admin (demo)")

    if admin_ok:
        db = get_db()
        ensure_demo_data(db)

        st.markdown("#### Users")
        with st.form("add_user_form"):
            name = st.text_input("Name")
            email = st.text_input("Email")
            role = st.selectbox("Role", ["admin", "instructor"])
            add_u = st.form_submit_button("Add user")
        if add_u:
            if not name or not email:
                st.error("Name and email required.")
            else:
                if db.query(User).filter_by(email=email).first():
                    st.warning("User already exists.")
                else:
                    db.add(User(name=name, email=email, role=role)); db.commit()
                    st.success("User added.")

        st.markdown("#### Courses")
        with st.form("add_course_form"):
            code = st.text_input("Course code")
            title = st.text_input("Course title")
            add_c = st.form_submit_button("Add course")
        if add_c:
            if not code or not title:
                st.error("Code and title required.")
            else:
                if db.query(Course).filter_by(code=code).first():
                    st.warning("Course already exists.")
                else:
                    db.add(Course(code=code, title=title)); db.commit()
                    st.success("Course added.")

        st.markdown("#### Assign Instructor to Course")
        users = db.query(User).filter_by(role="instructor").all()
        courses = db.query(Course).all()
        if users and courses:
            u_sel = st.selectbox("Instructor", users, format_func=lambda u: f"{u.name} ({u.email})")
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

# ----------------------------------
# Reports (quick global view)
# ----------------------------------
with tabs[3]:
    st.subheader("Reports")
    db = get_db()
    q = db.query(Attendance).join(Session, Attendance.session_id == Session.id).join(Course, Session.course_id == Course.id)
    rows = q.all()
    if rows:
        data = []
        for r in rows:
            data.append({
                "course": r.session.course.code,
                "course_title": r.session.course.title,
                "session_id": r.session.id,
                "check_in_at": r.created_at.astimezone().strftime('%Y-%m-%d %H:%M:%S'),
                "student_name": r.student_name,
                "student_email": r.student_email,
            })
        df = pd.DataFrame(data).sort_values(by=["course", "session_id", "check_in_at"])
        st.dataframe(df)
        csv = df.to_csv(index=False).encode()
        st.download_button("Download all attendance (CSV)", data=csv, file_name="attendance_all.csv", mime="text/csv")
    else:
        st.info("No attendance records yet.")
