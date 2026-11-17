import csv
import hashlib
import hmac
import os
import secrets
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import wraps
from hmac import compare_digest
from io import StringIO
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, abort, redirect, render_template, request, session, url_for
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
if os.environ.get("TRUST_PROXY") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

admin_password = os.environ.get("ADMIN_PASSWORD")
if not admin_password:
    raise RuntimeError("Set ADMIN_PASSWORD in the server environment before starting the app.")

secret_key = os.environ.get("FLASK_SECRET_KEY")
if not secret_key:
    raise RuntimeError("Set FLASK_SECRET_KEY in the server environment before starting the app.")

try:
    feedback_retention_days = int(os.environ.get("FEEDBACK_RETENTION_DAYS", "90"))
except ValueError as error:
    raise RuntimeError("FEEDBACK_RETENTION_DAYS must be a positive whole number.") from error
if feedback_retention_days < 1:
    raise RuntimeError("FEEDBACK_RETENTION_DAYS must be a positive whole number.")

app.config.update(
    SECRET_KEY=secret_key,
    ADMIN_USERNAME=os.environ.get("ADMIN_USERNAME", "admin"),
    ADMIN_PASSWORD=admin_password,
    FEEDBACK_RETENTION_DAYS=feedback_retention_days,
    MAX_CONTENT_LENGTH=16 * 1024,
    WTF_CSRF_ENABLED=True,
    WTF_CSRF_CHECK_DEFAULT=True,
    WTF_CSRF_TIME_LIMIT=None,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() == "true",
)
def default_data_directory():
    configured_directory = os.environ.get("FEEDBACKHUB_DATA_DIR")
    if configured_directory:
        return Path(configured_directory).expanduser().resolve()

    if os.name == "nt":
        local_app_data = Path(
            os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
        )
        return local_app_data / "FeedbackHub"

    data_home = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    )
    return data_home / "feedbackhub"


DATA_DIRECTORY = default_data_directory()
DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
DATABASE = DATA_DIRECTORY / "feedbackhub.sqlite3"
BACKUP_DIRECTORY = DATA_DIRECTORY / "backups"
LEGACY_DATABASE = Path(__file__).with_name("database.db")
RATE_LIMITS = {
    "admin_login": (5, 15 * 60),
    "feedback_submission": (10, 60 * 60),
}


def is_same_origin(request_obj):
    origin = request_obj.headers.get("Origin")
    referer = request_obj.headers.get("Referer")

    supplied_headers = [value for value in (origin, referer) if value]
    if not supplied_headers:
        return False

    expected_origin = urlparse(request_obj.host_url)
    for header_value in supplied_headers:
        parsed = urlparse(header_value)
        if (
            parsed.scheme != expected_origin.scheme
            or parsed.netloc != expected_origin.netloc
        ):
            return False

    return True


@app.before_request
def enforce_csrf():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return None

    origin = request.headers.get("Origin")
    referer = request.headers.get("Referer")

    if not origin and not referer:
        abort(403, description="CSRF validation failed.")

    if not is_same_origin(request):
        abort(403, description="CSRF validation failed.")

    token = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRFToken")
        or request.headers.get("X-CSRF-Token")
    )
    expected_token = session.get("csrf_token")

    if not token or not expected_token or not compare_digest(token, expected_token):
        abort(403, description="CSRF validation failed.")

    return None


@app.after_request
def add_security_headers(response):
    response.headers.update(
        {
            "Content-Security-Policy": (
                "default-src 'self'; base-uri 'self'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'"
            ),
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
    )
    if request.path.startswith("/admin") or request.path == "/export-csv":
        response.headers["Cache-Control"] = "no-store"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": session.get("csrf_token", "")}


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped_view


def connect_database():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def migrate_legacy_database():
    if DATABASE.exists() or not LEGACY_DATABASE.exists():
        return

    with tempfile.NamedTemporaryFile(
        prefix=".feedback-migration-",
        suffix=".sqlite3",
        dir=DATA_DIRECTORY,
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)

    try:
        with closing(sqlite3.connect(LEGACY_DATABASE)) as source:
            with closing(sqlite3.connect(temporary_path)) as destination:
                source.backup(destination)
                destination.commit()

        with closing(sqlite3.connect(temporary_path)) as migrated:
            integrity = migrated.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(
                    f"Migrated database failed integrity check: {integrity}"
                )

            tables = {
                row[0]
                for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "feedback" not in tables:
                raise RuntimeError(
                    "Legacy database has no feedback table; migration stopped."
                )

            feedback_count = migrated.execute(
                "SELECT COUNT(*) FROM feedback"
            ).fetchone()[0]

        os.replace(temporary_path, DATABASE)
    finally:
        temporary_path.unlink(missing_ok=True)

    LEGACY_DATABASE.unlink()
    app.logger.info(
        "Moved %s feedback records from the project folder to %s",
        feedback_count,
        DATABASE,
    )


def initialize_database():
    with closing(connect_database()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                comments TEXT NOT NULL,
                date_submitted TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS request_rate_limits (
                bucket TEXT NOT NULL,
                occurred_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_request_rate_limits_bucket_time
            ON request_rate_limits (bucket, occurred_at)
            """
        )
        connection.commit()


def purge_expired_feedback():
    retention_cutoff = (
        datetime.now() - timedelta(days=app.config["FEEDBACK_RETENTION_DAYS"])
    ).strftime("%Y-%m-%d %H:%M:%S")
    with closing(connect_database()) as connection:
        cursor = connection.execute(
            "DELETE FROM feedback WHERE date_submitted < ?",
            (retention_cutoff,),
        )
        deleted_count = cursor.rowcount
        connection.commit()
    if deleted_count:
        app.logger.info(
            "Deleted %s feedback records older than the configured retention period.",
            deleted_count,
        )
    return deleted_count


def backup_database():
    BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    backup_name = f"feedback-{datetime.now(timezone.utc):%Y-%m-%d}.sqlite3"
    backup_path = BACKUP_DIRECTORY / backup_name

    with tempfile.NamedTemporaryFile(
        prefix=".feedback-backup-",
        suffix=".sqlite3",
        dir=BACKUP_DIRECTORY,
        delete=False,
    ) as temporary_file:
        temporary_path = Path(temporary_file.name)

    try:
        with closing(sqlite3.connect(DATABASE)) as source:
            with closing(sqlite3.connect(temporary_path)) as destination:
                source.backup(destination)
                destination.commit()

        with closing(sqlite3.connect(temporary_path)) as verification:
            integrity = verification.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Database backup failed integrity check: {integrity}")

        os.replace(temporary_path, backup_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for old_backup in BACKUP_DIRECTORY.glob("feedback-????-??-??.sqlite3"):
        modified_at = datetime.fromtimestamp(
            old_backup.stat().st_mtime,
            timezone.utc,
        )
        if modified_at < cutoff:
            old_backup.unlink()

    return backup_path


def verify_backup(backup_path):
    BACKUP_DIRECTORY.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(backup_path)) as source:
        integrity = source.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup failed integrity check: {integrity}")

        tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "feedback" not in tables:
            raise RuntimeError("Backup is missing the feedback table.")

        expected_count = source.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        with tempfile.TemporaryDirectory(dir=BACKUP_DIRECTORY) as restore_directory:
            restored_path = Path(restore_directory) / "restored.sqlite3"
            with closing(sqlite3.connect(restored_path)) as restored:
                source.backup(restored)
                restored.commit()

            with closing(sqlite3.connect(restored_path)) as restored:
                restored_integrity = restored.execute("PRAGMA quick_check").fetchone()[0]
                restored_count = restored.execute(
                    "SELECT COUNT(*) FROM feedback"
                ).fetchone()[0]

    if restored_integrity != "ok" or restored_count != expected_count:
        raise RuntimeError("Backup restore verification did not match the source.")

    return restored_count


migrate_legacy_database()
initialize_database()
purge_expired_feedback()


def check_rate_limit(scope):
    limit, window_seconds = RATE_LIMITS[scope]
    now = int(datetime.now().timestamp())
    cutoff = now - window_seconds
    address = request.remote_addr or "unknown"
    address_digest = hmac.new(
        app.config["SECRET_KEY"].encode(),
        address.encode(),
        hashlib.sha256,
    ).hexdigest()
    bucket = f"{scope}:{address_digest}"

    with closing(connect_database()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM request_rate_limits WHERE occurred_at <= ?",
            (cutoff,),
        )
        recent_hits = connection.execute(
            """
            SELECT COUNT(*) AS hit_count, MIN(occurred_at) AS oldest_hit
            FROM request_rate_limits
            WHERE bucket = ? AND occurred_at > ?
            """,
            (bucket, cutoff),
        ).fetchone()

        if recent_hits["hit_count"] >= limit:
            retry_after = max(
                1,
                recent_hits["oldest_hit"] + window_seconds - now,
            )
            connection.rollback()
            return retry_after

        connection.execute(
            "INSERT INTO request_rate_limits (bucket, occurred_at) VALUES (?, ?)",
            (bucket, now),
        )
        connection.commit()

    return None


def rate_limited_response(scope):
    retry_after = check_rate_limit(scope)
    if retry_after is None:
        return None

    response = Response(
        "Too many requests. Please wait before trying again.",
        status=429,
        mimetype="text/plain",
    )
    response.headers["Retry-After"] = str(retry_after)
    return response


def make_spreadsheet_safe(value):
    text = str(value)
    formula_candidate = text.lstrip(" \t\r\n\v\f")
    if formula_candidate.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


@app.get("/")
def feedback_form():
    return render_template("index.html")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    error = None
    if request.method == "POST":
        limited_response = rate_limited_response("admin_login")
        if limited_response:
            return limited_response

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (
            compare_digest(username, app.config["ADMIN_USERNAME"])
            and compare_digest(password, app.config["ADMIN_PASSWORD"])
        ):
            session.clear()
            session["admin_authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin_dashboard"))
        error = "Invalid username or password."

    return render_template("admin_login.html", error=error)


@app.post("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.post("/submit-feedback")
def submit_feedback():
    limited_response = rate_limited_response("feedback_submission")
    if limited_response:
        return limited_response

    purge_expired_feedback()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()
    comments = request.form.get("comments", "").strip()
    rating_value = request.form.get("rating", "")

    try:
        rating = int(rating_value)
    except ValueError:
        rating = 0

    if not name or not email or not comments or not 1 <= rating <= 5:
        return render_template(
            "index.html",
            error="Please complete every field and choose a rating from 1 to 5.",
        ), 400

    with closing(connect_database()) as connection:
        connection.execute(
            """
            INSERT INTO feedback (name, email, rating, comments, date_submitted)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, email, rating, comments, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()

    return render_template("index.html", submitted=True)


@app.get("/admin")
@admin_required
def admin_dashboard():
    purge_expired_feedback()
    with closing(connect_database()) as connection:
        stats = connection.execute(
            """
            SELECT
                COUNT(*) AS total_feedback,
                COALESCE(ROUND(AVG(rating), 1), 0) AS average_rating,
                COALESCE(SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END), 0) AS five_star
            FROM feedback
            """
        ).fetchone()
        feedbacks = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM feedback ORDER BY id DESC LIMIT 10"
            ).fetchall()
        ]
        rating_counts = {
            row["rating"]: row["count"]
            for row in connection.execute(
                "SELECT rating, COUNT(*) AS count FROM feedback GROUP BY rating"
            ).fetchall()
        }

    return render_template(
        "admin.html",
        total_feedback=stats["total_feedback"],
        average_rating=stats["average_rating"],
        five_star=stats["five_star"],
        recent_feedback=len(feedbacks),
        feedbacks=feedbacks,
        rating_distribution=[rating_counts.get(rating, 0) for rating in range(1, 6)],
    )


@app.get("/export-csv")
@admin_required
def export_csv():
    purge_expired_feedback()
    with closing(connect_database()) as connection:
        feedbacks = connection.execute(
            "SELECT id, name, email, rating, comments, date_submitted "
            "FROM feedback ORDER BY id DESC"
        ).fetchall()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(("ID", "Name", "Email", "Rating", "Comments", "Date submitted"))
    writer.writerows(
        tuple(make_spreadsheet_safe(value) for value in row)
        for row in feedbacks
    )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=feedback.csv"},
    )


if __name__ == "__main__":
    backup_database()
    serve(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
    )
