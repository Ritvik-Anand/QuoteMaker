import functools
from flask import session, redirect, url_for, request
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page", next=request.path))
        if not session.get("is_admin"):
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


# is_admin always implies full access to the attendance/payroll feature too —
# "ops" is a single non-admin role covering both marking attendance and
# running payroll (previously two separate roles, merged into one).
def is_ops() -> bool:
    return bool(session.get("is_admin")) or session.get("role") == "ops"


# Kept as separate names since they gate conceptually different pages, even
# though both currently resolve to the same is_ops() check.
can_mark_attendance = is_ops
can_view_payroll_queue = is_ops


def ops_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login_page", next=request.path))
        if not is_ops():
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


marker_required = ops_required
payroll_viewer_required = ops_required


def verify_user(username: str, password: str):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()
    conn.close()
    if user and check_password_hash(user["password_hash"], password):
        return dict(user)
    return None


def create_user(username: str, password: str, is_admin: bool = False, role: str = "staff"):
    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, is_admin, role) VALUES (?,?,?,?)",
        (username.strip(), generate_password_hash(password, method="pbkdf2:sha256"), int(is_admin), role)
    )
    conn.commit()
    conn.close()


def change_password(user_id: int, new_password: str):
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password, method="pbkdf2:sha256"), user_id)
    )
    conn.commit()
    conn.close()
