from flask import Flask, render_template, request, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from pathlib import Path
from sqlalchemy import text, case, update , inspect
from datetime import datetime, timedelta
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from email.message import EmailMessage
from datetime import datetime, timedelta
import smtplib
import os
import json
import threading
import time

app = Flask(__name__)

# Local development settings
app.config["SECRET_KEY"] = "dev-only-change-before-deployment"
app.config["SQLALCHEMY_DATABASE_URI"] = (
    "sqlite:///" + str(Path(app.root_path) / "smart_components.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# Database model for users
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(254), nullable=True)
    approval_status = db.Column(db.String(20), nullable=False, default="Approved")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
class Component(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    part_number = db.Column(db.String(50), unique=True, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=0)
    # Returned damaged items are tracked separately and are never available to issue.
    damaged_quantity = db.Column(db.Integer, nullable=False, default=0)
    # Items awaiting inspection are also excluded from available stock.
    inspection_quantity = db.Column(db.Integer, nullable=False, default=0)
    location = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(300), default="")

# Database model for component requests
class ComponentRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    student_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False
    )

    component_id = db.Column(
        db.Integer,
        db.ForeignKey("component.id"),
        nullable=False
    )


    quantity = db.Column(db.Integer, nullable=False)
    
    # Quantity approved by Faculty and reserved
    approved_quantity = db.Column(
        db.Integer, nullable=False, default=0
    )

    # Quantity physically issued to the student
    issued_quantity = db.Column(
        db.Integer, nullable=False, default=0
    )

    purpose = db.Column(db.String(300), nullable=False)

    status = db.Column(
        db.String(20),
        nullable=False,
        default="Pending"
    )

    request_date = db.Column(
        db.DateTime,
        nullable=False,
        default=db.func.now()
    )
    request_date = db.Column(
        db.DateTime,
        nullable=False,
        default=db.func.now()
    )

    # Student's expected return date
    return_date = db.Column(
        db.Date,
        nullable=True
    )

    issue_window_started_at = db.Column(
        db.DateTime,
        nullable=True,
        default=None
    )
    issue_window_started_at = db.Column(
        db.DateTime,
        nullable=True,
        default=None
    )

    issue_window_expires_at = db.Column(
        db.DateTime,
        nullable=True,
        default=None
    )

# Database model for Admin-controlled system settings
class SystemSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(100), nullable=False, default="False")
    
# Records each successful component issue transaction
class IssueTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    request_id = db.Column(
        db.Integer,
        db.ForeignKey("component_request.id"),
        nullable=False
    )

    component_id = db.Column(
        db.Integer,
        db.ForeignKey("component.id"),
        nullable=False
    )

    faculty_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False
    )

    quantity = db.Column(
        db.Integer,
        nullable=False
    )

    issue_date = db.Column(
        db.DateTime,
        nullable=False,
        default=db.func.now()
    )
     
    issue_window_started_at = db.Column(
    db.DateTime,
    nullable=True
    )

    issue_window_expires_at = db.Column(
    db.DateTime,
    nullable=True
    )


# Records each partial or complete return against its original issue transaction.
class ReturnTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    issue_transaction_id = db.Column(
        db.Integer,
        db.ForeignKey("issue_transaction.id"),
        nullable=False
    )
    faculty_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=False
    )
    quantity = db.Column(db.Integer, nullable=False)
    disposition = db.Column(db.String(20), nullable=False, default="good")
    component_verified = db.Column(db.Boolean, nullable=False, default=False)
    physical_condition_checked = db.Column(db.Boolean, nullable=False, default=False)
    accessories_checked = db.Column(db.Boolean, nullable=False, default=False)
    tested_working = db.Column(db.Boolean, nullable=False, default=False)
    condition_notes = db.Column(db.String(500), nullable=True)
    return_date = db.Column(
        db.DateTime,
        nullable=False,
        default=db.func.now()
    )


# Prevent a due-date reminder from being sent more than once per request.
class DueReminderLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("component_request.id"), unique=True, nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class DueReminderRecipientLog(db.Model):
    """Tracks each reminder stage and recipient to prevent duplicate emails."""
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("component_request.id"), nullable=False)
    stage = db.Column(db.String(30), nullable=False)
    recipient_email = db.Column(db.String(254), nullable=False)
    sent_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint(
            "request_id", "stage", "recipient_email",
            name="uq_due_reminder_request_stage_recipient"
        ),
    )


def create_demo_users():
    demo_users = [
        ("admin", "Admin@123", "Admin"),
        ("faculty", "Faculty@123", "Faculty"),
        ("student", "Student@123", "Student"),
    ]

    for username, password, role in demo_users:
        existing_user = User.query.filter_by(
            username=username
        ).first()

        if existing_user is None:
            user = User(
                username=username,
                password_hash=generate_password_hash(password),
                role=role,
                approval_status="Approved",
                is_active=True,
            )
            db.session.add(user)

    db.session.commit()


with app.app_context():
    db.create_all()

    # Add account workflow columns safely to existing databases.
    user_columns = {row[1] for row in db.session.execute(text("PRAGMA table_info(user)")).fetchall()}
    with db.engine.begin() as connection:
        if "email" not in user_columns:
            connection.exec_driver_sql("ALTER TABLE user ADD COLUMN email VARCHAR(254)")
        if "approval_status" not in user_columns:
            connection.exec_driver_sql("ALTER TABLE user ADD COLUMN approval_status VARCHAR(20) NOT NULL DEFAULT 'Approved'")
        if "is_active" not in user_columns:
            connection.exec_driver_sql("ALTER TABLE user ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1")

    # Check columns in the existing SQLite table.
    columns = {
        row[1]
        for row in db.session.execute(
            text("PRAGMA table_info(component_request)")
        ).fetchall()
    }

    # Add approved_quantity if it is missing.
    if "approved_quantity" not in columns:
        db.session.execute(text(
            "ALTER TABLE component_request "
            "ADD COLUMN approved_quantity INTEGER "
            "NOT NULL DEFAULT 0"
        ))

    # Add issued_quantity if it is missing.
    if "issued_quantity" not in columns:
        db.session.execute(text(
            "ALTER TABLE component_request "
            "ADD COLUMN issued_quantity INTEGER "
            "NOT NULL DEFAULT 0"
        ))
        # Add return_date column safely if it does not exist.
    if "return_date" not in columns:
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE component_request ADD COLUMN return_date DATE"
            )

    # Add inventory condition columns to existing component tables.
    component_columns = {
        row[1]
        for row in db.session.execute(
            text("PRAGMA table_info(component)")
        ).fetchall()
    }
    with db.engine.begin() as connection:
        if "damaged_quantity" not in component_columns:
            connection.exec_driver_sql(
                "ALTER TABLE component ADD COLUMN damaged_quantity INTEGER NOT NULL DEFAULT 0"
            )
        if "inspection_quantity" not in component_columns:
            connection.exec_driver_sql(
                "ALTER TABLE component ADD COLUMN inspection_quantity INTEGER NOT NULL DEFAULT 0"
            )

    # Add inspection fields if this database already has return_transaction.
    return_columns = {
        row[1]
        for row in db.session.execute(
            text("PRAGMA table_info(return_transaction)")
        ).fetchall()
    }
    with db.engine.begin() as connection:
        if return_columns:
            if "disposition" not in return_columns:
                connection.exec_driver_sql("ALTER TABLE return_transaction ADD COLUMN disposition VARCHAR(20) NOT NULL DEFAULT 'good'")
            if "component_verified" not in return_columns:
                connection.exec_driver_sql("ALTER TABLE return_transaction ADD COLUMN component_verified BOOLEAN NOT NULL DEFAULT 0")
            if "physical_condition_checked" not in return_columns:
                connection.exec_driver_sql("ALTER TABLE return_transaction ADD COLUMN physical_condition_checked BOOLEAN NOT NULL DEFAULT 0")
            if "accessories_checked" not in return_columns:
                connection.exec_driver_sql("ALTER TABLE return_transaction ADD COLUMN accessories_checked BOOLEAN NOT NULL DEFAULT 0")
            if "tested_working" not in return_columns:
                connection.exec_driver_sql("ALTER TABLE return_transaction ADD COLUMN tested_working BOOLEAN NOT NULL DEFAULT 0")
            if "condition_notes" not in return_columns:
                connection.exec_driver_sql("ALTER TABLE return_transaction ADD COLUMN condition_notes VARCHAR(500)")

    db.session.commit()

    create_demo_users()


@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


def send_account_email(recipient, subject, body):
    """Send email only when SMTP settings are configured in environment."""
    host = os.environ.get("MAIL_SERVER")
    username = os.environ.get("MAIL_USERNAME")
    password = os.environ.get("MAIL_PASSWORD")
    sender = os.environ.get("MAIL_DEFAULT_SENDER", username or "")
    if not all([host, username, password, sender, recipient]):
        app.logger.warning("Account email not sent: configure MAIL_SERVER, MAIL_USERNAME, MAIL_PASSWORD, and MAIL_DEFAULT_SENDER.")
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content(body)
    port = int(os.environ.get("MAIL_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)
    return True


def get_mail_setting(key, default):
    row = SystemSetting.query.filter_by(key=key).first()
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return default


def configured_role_recipients(role_names):
    recipients = []
    for role_name in role_names:
        users = User.query.filter(
            db.func.lower(User.role) == role_name.lower(),
            User.is_active.is_(True),
            User.email.isnot(None),
            User.email != ""
        ).all()
        recipients.extend(user.email.strip().lower() for user in users if user.email)
    return sorted(set(recipients))


def send_configured_notification(setting_prefix, subject, body):
    roles = get_mail_setting(setting_prefix + "_recipients", [])
    sent = 0
    for recipient in configured_role_recipients(roles):
        try:
            sent += bool(send_account_email(recipient, subject, body))
        except Exception:
            app.logger.exception("Configured notification email failed for %s", recipient)
    return sent


def send_issue_notification_emails(component_request, component, student, faculty, issue_quantity, new_status):
    """Notify the student, issuing faculty member, and admins after a successful issue."""
    recipients = []
    if student and student.email:
        recipients.append((student.email.strip().lower(), "Student"))
    if faculty and faculty.email:
        recipients.append((faculty.email.strip().lower(), "Issuing faculty"))
    admins = User.query.filter(
        db.func.lower(User.role) == "admin",
        User.is_active.is_(True),
        User.email.isnot(None),
        User.email != ""
    ).all()
    for admin in admins:
        recipients.append((admin.email.strip().lower(), "Admin"))

    # Avoid sending duplicate messages if accounts share an email address.
    unique_recipients = {}
    for email, role_label in recipients:
        if email:
            unique_recipients.setdefault(email, []).append(role_label)

    issue_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"Component issued: {component.name} (Request #{component_request.id})"
    body = (
        "A component issue has been recorded in the Smart Component System.\n\n"
        f"Request ID: {component_request.id}\n"
        f"Student: {student.username if student else 'Unknown'}\n"
        f"Student email: {student.email if student and student.email else 'Not registered'}\n"
        f"Issuing faculty: {faculty.username if faculty else 'Unknown'}\n"
        f"Component: {component.name}\n"
        f"Part number: {component.part_number}\n"
        f"Quantity issued in this transaction: {issue_quantity}\n"
        f"Total quantity issued for request: {component_request.issued_quantity}\n"
        f"Request status: {new_status}\n"
        f"Purpose: {component_request.purpose}\n"
        f"Issue recorded at: {issue_time}\n\n"
        "This is an automatic notification; no reply is required."
    )
    sent_count = 0
    failed_count = 0
    for email in unique_recipients:
        try:
            if send_account_email(email, subject, body):
                sent_count += 1
            else:
                failed_count += 1
        except Exception:
            failed_count += 1
            app.logger.exception("Issue notification email failed for recipient %s", email)
    return sent_count, failed_count


@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "").strip().title()
        if not username or not email or not password or role not in ("Student", "Faculty"):
            error = "Please complete all fields and select Student or Faculty."
        elif User.query.filter_by(username=username).first():
            error = "That username is already registered."
        elif User.query.filter(db.func.lower(User.email) == email).first():
            error = "That email address is already registered."
        else:
            user = User(username=username, email=email,
                        password_hash=generate_password_hash(password),
                        role=role, approval_status="Pending", is_active=False)
            db.session.add(user)
            db.session.commit()
            flash("Signup submitted. You can log in after the admin approves your account.", "success")
            return redirect(url_for("login"))
    return render_template("signup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            if user.approval_status != "Approved":
                error = "Your account is pending admin approval." if user.approval_status == "Pending" else "Your account has not been approved. Contact the administrator."
            elif not user.is_active:
                error = "Your account has been revoked or disabled. Contact the administrator."
            else:
                session.clear()
                session["user_id"] = user.id
                session["username"] = user.username
                session["role"] = user.role
                return redirect(url_for("dashboard"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/admin/account-approvals", methods=["GET", "POST"])
def account_approvals():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if not admin_required():
        return "Access denied: Admin only.", 403
    if request.method == "POST":
        user_id = request.form.get("user_id", type=int)
        action = request.form.get("action", "")
        user = db.session.get(User, user_id) if user_id else None
        if not user or user.role not in ("Student", "Faculty"):
            flash("Account not found.", "danger")
        elif action == "approve":
            user.approval_status = "Approved"
            user.is_active = True
            db.session.commit()
            try:
                sent = send_account_email(user.email, "Smart Component System account approved",
                    f"Hello {user.username},\n\nYour {user.role.lower()} account has been approved. You can now log in normally.")
                flash("Account approved." + (" Approval email sent." if sent else " Email not sent; configure SMTP settings."), "success")
            except Exception:
                app.logger.exception("Approval email failed")
                flash("Account approved, but the email could not be sent. Check SMTP settings.", "warning")
        elif action == "reject":
            user.approval_status = "Rejected"
            user.is_active = False
            db.session.commit()
            try:
                sent = send_account_email(user.email, "Smart Component System signup decision",
                    f"Hello {user.username},\n\nYour signup request was not approved. Please contact the administrator if you need more information.")
                flash("Account rejected." + (" Notification email sent." if sent else " Email not sent; configure SMTP settings."), "success")
            except Exception:
                app.logger.exception("Rejection email failed")
                flash("Account rejected, but the email could not be sent. Check SMTP settings.", "warning")
        elif action == "revoke":
            user.is_active = False
            db.session.commit()
            flash("Account access revoked. The user will be blocked on their next request.", "success")
        elif action == "restore":
            if user.approval_status == "Approved":
                user.is_active = True
                db.session.commit()
                flash("Account access restored.", "success")
        return redirect(url_for("account_approvals"))
    users = User.query.filter(User.role.in_(["Student", "Faculty"])).order_by(User.id.desc()).all()
    return render_template("admin_account_approvals.html", users=users)


@app.before_request
def enforce_account_status():
    if "user_id" not in session or request.endpoint in ("login", "signup", "logout", "static"):
        return None
    user = db.session.get(User, session.get("user_id"))
    if user is None or not user.is_active or user.approval_status != "Approved":
        session.clear()
        flash("Your account is not active. Please contact the administrator.", "warning")
        return redirect(url_for("login"))
    return None


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    # The dashboard template displays the signed-in user's details. This app
    # uses Flask's session directly (not Flask-Login), so explicitly load the
    # user and pass it to the template as current_user.
    current_user = db.session.get(User, session["user_id"])
    if current_user is None:
        session.clear()
        return redirect(url_for("login"))

    personal_requests = []
    personal_issues = []

    if current_user.role == "Student":
        requests_for_user = ComponentRequest.query.filter_by(
            student_id=current_user.id
        ).order_by(ComponentRequest.request_date.desc()).all()
        for component_request in requests_for_user:
            personal_requests.append({
                "request": component_request,
                "component": db.session.get(Component, component_request.component_id),
            })

    elif current_user.role == "Faculty":
        transactions = IssueTransaction.query.filter_by(
            faculty_id=current_user.id
        ).order_by(IssueTransaction.issue_date.desc()).all()
        for transaction in transactions:
            component_request = db.session.get(ComponentRequest, transaction.request_id)
            student = db.session.get(User, component_request.student_id) if component_request else None
            personal_issues.append({
                "transaction": transaction,
                "component": db.session.get(Component, transaction.component_id),
                "student": student,
            })

    return render_template(
        "dashboard.html",
        username=current_user.username,
        role=current_user.role,
        current_user=current_user,
        personal_requests=personal_requests,
        personal_issues=personal_issues,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


from flask import flash

def admin_required():
    return "user_id" in session and session.get("role") == "Admin"


@app.route("/inventory", methods=["GET", "POST"])
def inventory():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not admin_required():
        return "Access denied: Admin only.", 403

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        part_number = request.form.get("part_number", "").strip()
        category = request.form.get("category", "").strip()
        location = request.form.get("location", "").strip()
        description = request.form.get("description", "").strip()

        try:
            quantity = int(request.form.get("quantity", "0"))
        except ValueError:
            quantity = -1

        if not all([name, part_number, category, location]):
            flash("Please fill in all required fields.", "danger")
        elif quantity < 0:
            flash("Quantity cannot be negative.", "danger")
        elif Component.query.filter_by(part_number=part_number).first():
            flash("That part number already exists.", "danger")
        else:
            component = Component(
                name=name,
                part_number=part_number,
                category=category,
                quantity=quantity,
                location=location,
                description=description
            )
            db.session.add(component)
            db.session.commit()
            flash("Component added successfully!", "success")
            return redirect(url_for("inventory"))

    components = Component.query.order_by(Component.name).all()
    return render_template("inventory.html", components=components)


# Edit an existing component
@app.route("/inventory/edit/<int:component_id>", methods=["GET", "POST"])
def edit_component(component_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not admin_required():
        return "Access denied: Admin only.", 403

    component = db.session.get(Component, component_id)

    if component is None:
        return "Component not found.", 404

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        part_number = request.form.get("part_number", "").strip()
        category = request.form.get("category", "").strip()
        location = request.form.get("location", "").strip()
        description = request.form.get("description", "").strip()

        try:
            quantity = int(request.form.get("quantity", ""))
        except ValueError:
            quantity = -1

        if not all([name, part_number, category, location]):
            flash("Please fill in all required fields.", "danger")

        elif quantity < 0:
            flash("Quantity cannot be negative.", "danger")

        elif Component.query.filter(
            Component.part_number == part_number,
            Component.id != component.id
        ).first():
            flash("That part number already belongs to another component.",
                  "danger")

        else:
            component.name = name
            component.part_number = part_number
            component.category = category
            component.quantity = quantity
            component.location = location
            component.description = description

            db.session.commit()

            flash("Component updated successfully!", "success")
            return redirect(url_for("inventory"))

    return render_template(
        "edit_component.html",
        component=component
    )


# Delete a component (POST only)
@app.route("/inventory/delete/<int:component_id>", methods=["POST"])
def delete_component(component_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not admin_required():
        return "Access denied: Admin only.", 403

    component = db.session.get(Component, component_id)

    if component is None:
        flash("Component not found.", "warning")
        return redirect(url_for("inventory"))

    db.session.delete(component)
    db.session.commit()

    flash("Component deleted successfully.", "success")
    return redirect(url_for("inventory"))

# Student component request page
@app.route("/component-request", methods=["GET", "POST"])
def component_request():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "Student":
        return "Access denied: Students only.", 403

    today = datetime.now().date()
    max_return_date = today + timedelta(days=10)

    if request.method == "POST":
        component_id = request.form.get("component_id", type=int)
        quantity = request.form.get("quantity", type=int)
        purpose = request.form.get("purpose", "").strip()
        selected_date = request.form.get("return_date", "").strip()

        # Use 10 days from today if the student leaves the date blank.
        if not selected_date:
            return_date = max_return_date
        else:
            try:
                return_date = datetime.strptime(
                    selected_date, "%Y-%m-%d"
                ).date()
            except ValueError:
                return_date = None

        component = (
            db.session.get(Component, component_id)
            if component_id is not None
            else None
        )

        if return_date is None:
            flash("Please enter a valid return date.", "danger")

        elif return_date < today or return_date > max_return_date:
            flash(
                "Return date must be within 10 days from today.",
                "danger"
            )

        elif component is None:
            flash("Please select a valid component.", "danger")

        elif quantity is None or quantity <= 0:
            flash("Quantity must be greater than zero.", "danger")

        elif quantity > component.quantity:
            flash("Requested quantity exceeds available stock.", "danger")

        elif not purpose:
            flash("Please enter the purpose of the request.", "danger")

        else:
            new_request = ComponentRequest(
                student_id=session["user_id"],
                component_id=component.id,
                quantity=quantity,
                purpose=purpose,
                return_date=return_date,
                status="Pending"
            )

            db.session.add(new_request)
            db.session.commit()

            flash("Request submitted successfully!", "success")
            return redirect(url_for("component_request"))

    components = Component.query.filter(
        Component.quantity > 0
    ).order_by(Component.name).all()

    component_requests = ComponentRequest.query.filter_by(
        student_id=session["user_id"]
    ).order_by(ComponentRequest.request_date.desc()).all()

    return render_template(
        "component_request.html",
        components=components,
        component_requests=component_requests,
        today=today.isoformat(),
        max_return_date=max_return_date.isoformat()
    )

# Admin control for Faculty access
@app.route("/admin/faculty-access", methods=["GET", "POST"])
def faculty_access():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not admin_required():
        return "Access denied: Admin only.", 403

    setting = SystemSetting.query.filter_by(
        key="faculty_access_enabled"
    ).first()
    
    # Create the setting on first visit; default is disabled
    if setting is None:
        setting = SystemSetting(
            key="faculty_access_enabled",
            value="False"
        )
        db.session.add(setting)
        db.session.commit()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "enable":
            setting.value = "True"
            db.session.commit()
            flash("Faculty access enabled.", "success")

        elif action == "disable":
            setting.value = "False"
            db.session.commit()
            flash("Faculty access disabled.", "warning")

        else:
            flash("Invalid action.", "danger")

        return redirect(url_for("faculty_access"))

    return render_template(
        "faculty_access.html",
        faculty_access_enabled=(setting.value == "True")
    )



@app.route("/faculty/requests", methods=["GET", "POST"])
def faculty_requests():
    # 1. Authentication
    if "user_id" not in session:
        return redirect(url_for("login"))

    # 2. Faculty-only access
    if session.get("role") != "Faculty":
        return "Access denied: Faculty only.", 403

    # 3. Admin must enable Faculty access
    setting = SystemSetting.query.filter_by(
        key="faculty_access_enabled"
    ).first()

    if setting is None or setting.value != "True":
        return "Faculty access is disabled by Admin.", 403

    # 4. Handle Faculty actions
    if request.method == "POST":
        request_id = request.form.get("request_id", type=int)
        action = request.form.get("action", "").strip()

        component_request = (
            db.session.get(ComponentRequest, request_id)
            if request_id is not None
            else None
        )

        if component_request is None:
            flash("Request not found.", "danger")
            return redirect(url_for("faculty_requests"))

        # --------------------------------------------------
        # ACTION A: APPROVE A REQUEST, INCLUDING PARTIAL APPROVAL
        # --------------------------------------------------
        if action == "approve":

            if component_request.status != "Pending":
                flash(
                    "Only pending requests can be approved.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            approved_quantity = request.form.get(
                "approved_quantity", type=int
            )

            if approved_quantity is None or approved_quantity <= 0:
                flash(
                    "Approved quantity must be greater than zero.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            if approved_quantity > component_request.quantity:
                flash(
                    "Approved quantity cannot exceed the requested quantity.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            component = db.session.get(
                Component, component_request.component_id
            )

            if component is None:
                flash(
                    "The requested component no longer exists.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            # Calculate quantities reserved by other approved requests.
            reserved_quantity = (
                db.session.query(
                    db.func.coalesce(
                        db.func.sum(
                            ComponentRequest.approved_quantity
                            - ComponentRequest.issued_quantity
                        ),
                        0
                    )
                )
                .filter(
                    ComponentRequest.component_id == component.id,
                    ComponentRequest.status.in_(
                        ["Pending Admin", "Approved", "Partially Issued"]
                    ),
                    ComponentRequest.approved_quantity
                    > ComponentRequest.issued_quantity
                )
                .scalar()
            ) or 0

            available_quantity = max(
                0, component.quantity - reserved_quantity
            )

            if approved_quantity > available_quantity:
                flash(
                    f"Only {available_quantity} component(s) are "
                    "available for approval after existing reservations.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            try:
                component_request.approved_quantity = approved_quantity
                component_request.issued_quantity = 0
                # Faculty approval is not the final approval. The request
                # must be reviewed by Admin before Faculty can issue it.
                component_request.status = "Pending Admin"

                db.session.commit()

                flash(
                    f"Faculty approval saved for {approved_quantity} component(s). "
                    "The request is now waiting for Admin approval. "
                    "Inventory has not been reduced.",
                    "success"
                )

            except Exception:
                db.session.rollback()
                flash(
                    "Approval failed. No changes were saved.",
                    "danger"
                )

            return redirect(url_for("faculty_requests"))

        # --------------------------------------------------
        # ACTION B: REJECT A PENDING REQUEST
        # --------------------------------------------------
        elif action == "reject":

            if component_request.status != "Pending":
                flash(
                    "Only pending requests can be rejected.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            try:
                component_request.status = "Rejected"
                db.session.commit()

                flash("Request rejected.", "warning")

            except Exception:
                db.session.rollback()
                flash(
                    "Rejection failed. No changes were saved.",
                    "danger"
                )

            return redirect(url_for("faculty_requests"))

        # --------------------------------------------------
        # ACTION C: ISSUE APPROVED COMPONENTS
        # Supports multiple partial issues.
        # --------------------------------------------------
        
        # ACTION: OPEN A TIME-LIMITED ISSUING WINDOW
        elif action == "start_issue_window":

            if component_request.status not in [
                "Approved", "Partially Issued"
            ]:
                flash(
                    "Only Admin-approved requests can open an issuing window.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            remaining_quantity = (
                (component_request.approved_quantity or 0)
                - (component_request.issued_quantity or 0)
            )

            if remaining_quantity <= 0:
                flash(
                    "This request has already been fully issued.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            duration = request.form.get(
                "duration_minutes", type=int
            )

            if duration not in [2, 5, 10]:
                flash(
                    "Select 2, 5, or 10 minutes.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            try:
                started_at = datetime.utcnow()

                component_request.issue_window_started_at = started_at
                component_request.issue_window_expires_at = (
                    started_at + timedelta(minutes=duration)
                )

                db.session.commit()

                # Signal the ESP32 when the issuing window opens. This is a
                # best-effort hardware notification; it must not undo the
                # issuing window or affect the SQLite transaction if the ESP32
                # is offline. Override ESP32_TRIGGER_URL if its IP changes.
                trigger_url = os.environ.get(
                    "ESP32_TRIGGER_URL",
                    "http://10.70.136.118/trigger"
                )
                try:
                    with urlopen(trigger_url, timeout=2) as esp32_response:
                        app.logger.info(
                            "ESP32 trigger returned HTTP %s",
                            esp32_response.status
                        )
                except (URLError, HTTPError, TimeoutError, OSError):
                    app.logger.exception(
                        "Issuing window opened, but ESP32 LED trigger failed"
                    )

                flash(
                    f"Issuing window opened for {duration} minute(s).",
                    "success"
                )

            except Exception:
                db.session.rollback()
                app.logger.exception(
                    "Could not open issuing window for request %s",
                    request_id
                )
                flash(
                    "Could not open the issuing window.",
                    "danger"
                )

            return redirect(url_for("faculty_requests"))

        elif action == "issue":

            if component_request.status not in ["Approved", "Partially Issued"]:
                flash(
                    "Only approved requests with outstanding quantities can be issued.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            # Validate the issuing window before changing anything.
            expires_at = component_request.issue_window_expires_at
            if expires_at is None:
                flash(
                    "Open an authorized issuing window before issuing components.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            # SQLite stores these values as naive datetimes, so compare
            # using the same naive UTC convention used when opening the window.
            if datetime.utcnow() >= expires_at:
                flash(
                    "The issuing window has expired. Open a new window to continue.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            try:
                issue_quantity = int(request.form.get("issue_quantity", ""))
            except (TypeError, ValueError):
                flash("Enter a valid whole-number issue quantity.", "danger")
                return redirect(url_for("faculty_requests"))

            approved_quantity = component_request.approved_quantity or 0
            previously_issued = component_request.issued_quantity or 0
            remaining_quantity = approved_quantity - previously_issued

            if remaining_quantity <= 0:
                flash("This request has already been fully issued.", "warning")
                return redirect(url_for("faculty_requests"))

            if issue_quantity <= 0:
                flash("Issue quantity must be greater than zero.", "danger")
                return redirect(url_for("faculty_requests"))

            if issue_quantity > remaining_quantity:
                flash(
                    f"You can issue at most {remaining_quantity} more component(s) for this request.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            component = db.session.get(Component, component_request.component_id)
            if component is None:
                flash("The requested component no longer exists.", "danger")
                return redirect(url_for("faculty_requests"))

            if component.quantity < issue_quantity:
                flash(
                    f"Insufficient inventory. Only {component.quantity} component(s) are in stock.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            # Calculate the final state BEFORE the database update. Do not
            # recalculate from component_request after a bulk UPDATE because
            # SQLAlchemy may synchronize that in-memory object automatically.
            new_issued_quantity = previously_issued + issue_quantity
            fully_issued = new_issued_quantity >= approved_quantity
            new_status = "Issued" if fully_issued else "Partially Issued"

            try:
                request_update = db.session.execute(
                    update(ComponentRequest)
                    .where(
                        ComponentRequest.id == component_request.id,
                        ComponentRequest.status.in_(["Approved", "Partially Issued"]),
                        (
                            ComponentRequest.approved_quantity
                            - ComponentRequest.issued_quantity
                        ) >= issue_quantity,
                        ComponentRequest.issue_window_expires_at.is_not(None),
                        ComponentRequest.issue_window_expires_at > datetime.utcnow()
                    )
                    .values(
                        issued_quantity=ComponentRequest.issued_quantity + issue_quantity,
                        status=new_status,
                        # Stop the current issuing timer after EVERY successful issue.
                        # Faculty can open a fresh window for any remaining quantity.
                        issue_window_started_at=None,
                        issue_window_expires_at=None
                    )
                    .execution_options(synchronize_session=False)
                )

                if request_update.rowcount != 1:
                    db.session.rollback()
                    flash(
                        "The request changed or the issuing window expired. Refresh the page and try again.",
                        "warning"
                    )
                    return redirect(url_for("faculty_requests"))

                stock_update = db.session.execute(
                    update(Component)
                    .where(
                        Component.id == component_request.component_id,
                        Component.quantity >= issue_quantity
                    )
                    .values(quantity=Component.quantity - issue_quantity)
                )

                if stock_update.rowcount != 1:
                    db.session.rollback()
                    flash(
                        "Insufficient inventory. No issue was recorded and stock was not changed.",
                        "danger"
                    )
                    return redirect(url_for("faculty_requests"))

                db.session.add(IssueTransaction(
                    request_id=component_request.id,
                    component_id=component_request.component_id,
                    faculty_id=session["user_id"],
                    quantity=issue_quantity,
                    issue_window_started_at=component_request.issue_window_started_at,
                    issue_window_expires_at=component_request.issue_window_expires_at
                ))

                db.session.commit()

                # Send notifications only after the issue and stock updates are committed.
                student_user = User.query.get(component_request.student_id)
                faculty_user = User.query.get(session["user_id"])
                app.logger.warning("ISSUE EMAIL DEBUG - faculty username: %s", faculty_user.username if faculty_user else None)
                app.logger.warning("ISSUE EMAIL DEBUG - faculty email: %s", repr(faculty_user.email if faculty_user else None))
                component_item = Component.query.get(component_request.component_id)
                sent_count, failed_count = send_issue_notification_emails(
                    component_request,
                    component_item,
                    student_user,
                    faculty_user,
                    issue_quantity,
                    new_status,
                )
                if sent_count:
                    flash(
                        f"Issue notification email sent to {sent_count} recipient(s).",
                        "info"
                    )
                if failed_count:
                    flash(
                        "The issue was saved, but one or more notification emails could not be sent. Check SMTP settings and application logs.",
                        "warning"
                    )

                if fully_issued:
                    flash(
                        f"Successfully issued {issue_quantity} component(s). Request marked Issued and the timer has been stopped.",
                        "success"
                    )
                else:
                    flash(
                        f"Successfully issued {issue_quantity} component(s). "
                        "Request marked Partially Issued. The timer has stopped; "
                        "open a new issuing window for the remaining quantity.",
                        "success"
                    )

            except Exception:
                db.session.rollback()
                app.logger.exception(
                    "Faculty component issue failed for request %s",
                    request_id
                )
                flash("Could not record the issue. No changes were saved.", "danger")

            return redirect(url_for("faculty_requests"))

        elif action == "return_component":
            issue_transaction_id = request.form.get(
                "issue_transaction_id", type=int
            )
            return_quantity = request.form.get("return_quantity", type=int)

            issue_transaction = (
                db.session.get(IssueTransaction, issue_transaction_id)
                if issue_transaction_id is not None else None
            )

            if (
                issue_transaction is None
                or issue_transaction.request_id != component_request.id
            ):
                flash("Issue transaction not found for this request.", "danger")
                return redirect(url_for("faculty_requests"))

            # Use the original issue timestamp and the same naive-UTC
            # convention used by the issuing workflow.
            issue_date = issue_transaction.issue_date
            if issue_date is None or datetime.utcnow() < (
                issue_date + timedelta(hours=24)
            ):
                flash(
                    "Components can only be returned after 24 hours "
                    "from their issue time.",
                    "warning"
                )
                return redirect(url_for("faculty_requests"))

            already_returned = db.session.query(
                db.func.coalesce(db.func.sum(ReturnTransaction.quantity), 0)
            ).filter_by(
                issue_transaction_id=issue_transaction.id
            ).scalar() or 0
            outstanding = issue_transaction.quantity - already_returned

            if return_quantity is None or return_quantity <= 0:
                flash("Enter a valid return quantity greater than zero.", "danger")
                return redirect(url_for("faculty_requests"))

            if return_quantity > outstanding:
                flash(
                    f"You can return at most {outstanding} component(s) "
                    "from this issue transaction.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            component = db.session.get(Component, issue_transaction.component_id)
            if component is None:
                flash("The component record could not be found.", "danger")
                return redirect(url_for("faculty_requests"))

            disposition = request.form.get("disposition", "").strip().lower()
            component_verified = request.form.get("component_verified") == "on"
            physical_condition_checked = request.form.get("physical_condition_checked") == "on"
            accessories_checked = request.form.get("accessories_checked") == "on"
            tested_working = request.form.get("tested_working") == "on"
            condition_notes = request.form.get("condition_notes", "").strip()[:500]

            if not (component_verified and physical_condition_checked and accessories_checked):
                flash("Complete all inspection checklist confirmations before recording the return.", "warning")
                return redirect(url_for("faculty_requests"))
            if disposition not in {"good", "damaged", "needs_testing"}:
                flash("Select a valid component condition.", "danger")
                return redirect(url_for("faculty_requests"))
            if disposition == "good" and not tested_working:
                flash("Confirm that the component was tested and is working before adding it to available stock.", "warning")
                return redirect(url_for("faculty_requests"))
            if disposition == "damaged" and not condition_notes:
                flash("Add a short note describing the damage.", "warning")
                return redirect(url_for("faculty_requests"))

            # Reuse the same ESP32 HTTP /trigger endpoint as the existing
            # issuing-window action. Save the return only if the ESP32 accepts
            # the trigger request.
            trigger_url = os.environ.get(
                "ESP32_TRIGGER_URL",
                "http://10.70.136.118/trigger"
            )
            try:
                with urlopen(trigger_url, timeout=2) as esp32_response:
                    if esp32_response.status != 200:
                        raise RuntimeError(
                            f"ESP32 returned HTTP {esp32_response.status}"
                        )
            except (URLError, HTTPError, TimeoutError, OSError, RuntimeError):
                app.logger.exception(
                    "ESP32 return trigger failed for issue transaction %s",
                    issue_transaction.id
                )
                flash(
                    "ESP32 did not confirm the return trigger. "
                    "No return was recorded and stock was not changed.",
                    "danger"
                )
                return redirect(url_for("faculty_requests"))

            try:
                db.session.add(ReturnTransaction(
                    issue_transaction_id=issue_transaction.id,
                    faculty_id=session["user_id"],
                    quantity=return_quantity,
                    disposition=disposition,
                    component_verified=component_verified,
                    physical_condition_checked=physical_condition_checked,
                    accessories_checked=accessories_checked,
                    tested_working=tested_working,
                    condition_notes=condition_notes or None
                ))

                if disposition == "good":
                    component.quantity += return_quantity
                    stock_message = "added back to available stock"
                elif disposition == "damaged":
                    component.damaged_quantity += return_quantity
                    stock_message = "recorded as damaged and excluded from available stock"
                else:
                    component.inspection_quantity += return_quantity
                    stock_message = "held for testing and excluded from available stock"

                db.session.commit()

                # Return notification uses its own admin-configured recipients.
                request_record = db.session.get(ComponentRequest, issue_transaction.request_id)
                student_record = db.session.get(User, request_record.student_id) if request_record else None
                component_name = component.name if component else "Component"
                return_body = (
                    "A component return has been recorded.\n\n"
                    f"Request ID: {issue_transaction.request_id}\n"
                    f"Component: {component_name}\n"
                    f"Student: {student_record.username if student_record else 'Unknown'}\n"
                    f"Quantity returned: {return_quantity}\n"
                    f"Condition: {disposition.replace('_', ' ').title()}\n"
                    f"Return date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Notes: {condition_notes or 'None'}\n"
                )
                send_configured_notification(
                    "return", f"Component return recorded: {component_name}", return_body
                )
                flash(
                    f"Recorded return of {return_quantity} component(s): {stock_message}.",
                    "success"
                )
            except Exception:
                db.session.rollback()
                app.logger.exception(
                    "Could not record return for issue transaction %s",
                    issue_transaction.id
                )
                flash(
                    "The ESP32 trigger was sent, but the database return "
                    "could not be saved. Check the inventory before retrying.",
                    "danger"
                )

            return redirect(url_for("faculty_requests"))

        else:
            flash("Invalid action.", "danger")
            return redirect(url_for("faculty_requests"))

    # 5. Display requests, newest first
    requests_list = ComponentRequest.query.order_by(
        ComponentRequest.request_date.desc()
    ).all()

    request_rows = []

    for item in requests_list:
        issue_items = []
        issue_transactions = IssueTransaction.query.filter_by(
            request_id=item.id
        ).order_by(IssueTransaction.issue_date.asc()).all()

        for transaction in issue_transactions:
            returned_quantity = db.session.query(
                db.func.coalesce(db.func.sum(ReturnTransaction.quantity), 0)
            ).filter_by(
                issue_transaction_id=transaction.id
            ).scalar() or 0
            outstanding_to_return = transaction.quantity - returned_quantity
            issue_date = transaction.issue_date
            return_eligible = (
                issue_date is not None
                and datetime.utcnow() >= issue_date + timedelta(hours=24)
                and outstanding_to_return > 0
            )
            if outstanding_to_return > 0:
                issue_items.append({
                    "transaction": transaction,
                    "outstanding": outstanding_to_return,
                    "eligible": return_eligible
                })

        request_rows.append({
            "request": item,
            "student": db.session.get(User, item.student_id),
            "component": db.session.get(Component, item.component_id),
            "return_items": issue_items
        })

    
    return render_template(
        "faculty_requests.html",
        request_rows=request_rows,
        now=datetime.utcnow()
    )


# Admin controls for due-date and return notification emails.
@app.route("/admin/email-reminders", methods=["GET", "POST"])
def admin_email_reminders():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if not admin_required():
        return "Access denied: Admin only.", 403

    if request.method == "POST":
        try:
            days = int(request.form.get("due_days_before", "2"))
            if days < 0 or days > 30:
                raise ValueError
        except ValueError:
            flash("Reminder lead time must be between 0 and 30 days.", "danger")
            return redirect(url_for("admin_email_reminders"))

        values = {
            "due_days_before": str(days),
            "due_recipients": json.dumps(request.form.getlist("due_recipients")),
            "return_recipients": json.dumps(request.form.getlist("return_recipients")),
        }
        for key, value in values.items():
            setting = SystemSetting.query.filter_by(key=key).first()
            if setting is None:
                setting = SystemSetting(key=key, value=value)
                db.session.add(setting)
            else:
                setting.value = value
        db.session.commit()
        flash("Email reminder settings saved.", "success")
        return redirect(url_for("admin_email_reminders"))

    return render_template(
        "admin_email_reminders.html",
        due_days_before=int(get_mail_setting("due_days_before", 2)),
        due_recipients=get_mail_setting("due_recipients", ["Admin", "Faculty"]),
        return_recipients=get_mail_setting("return_recipients", ["Admin", "Faculty"]),
    )


# Admin view of all component issue and partial-return transactions.
@app.route("/admin/component-tracking")
def admin_component_tracking():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not admin_required():
        return "Access denied: Admin only.", 403

    transactions = IssueTransaction.query.order_by(
        IssueTransaction.issue_date.desc(), IssueTransaction.id.desc()
    ).all()

    tracking_rows = []
    for transaction in transactions:
        component_request = db.session.get(ComponentRequest, transaction.request_id)
        component = db.session.get(Component, transaction.component_id)
        faculty = db.session.get(User, transaction.faculty_id)
        student = (
            db.session.get(User, component_request.student_id)
            if component_request is not None else None
        )
        returns = ReturnTransaction.query.filter_by(
            issue_transaction_id=transaction.id
        ).order_by(ReturnTransaction.return_date.asc(), ReturnTransaction.id.asc()).all()
        returned_quantity = sum(item.quantity for item in returns)
        outstanding_quantity = max(transaction.quantity - returned_quantity, 0)
        tracking_rows.append({
            "transaction": transaction,
            "request": component_request,
            "component": component,
            "faculty": faculty,
            "student": student,
            "returns": returns,
            "returned_quantity": returned_quantity,
            "outstanding_quantity": outstanding_quantity,
        })

    return render_template("admin_component_tracking.html", tracking_rows=tracking_rows)


# Admin review of requests already approved by Faculty.
@app.route("/admin/component-approvals", methods=["GET", "POST"])
def admin_component_approvals():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if not admin_required():
        return "Access denied: Admin only.", 403

    if request.method == "POST":
        request_id = request.form.get("request_id", type=int)
        action = request.form.get("action", "").strip().lower()
        component_request = (
            db.session.get(ComponentRequest, request_id)
            if request_id is not None else None
        )

        if component_request is None:
            flash("Request not found.", "danger")
            return redirect(url_for("admin_component_approvals"))

        if component_request.status != "Pending Admin":
            flash("Only faculty-approved requests awaiting Admin review can be decided.", "warning")
            return redirect(url_for("admin_component_approvals"))

        if action == "approve":
            component = db.session.get(Component, component_request.component_id)
            if component is None:
                flash("The requested component no longer exists.", "danger")
                return redirect(url_for("admin_component_approvals"))

            remaining = component_request.approved_quantity - component_request.issued_quantity
            reserved_quantity = (
                db.session.query(
                    db.func.coalesce(
                        db.func.sum(ComponentRequest.approved_quantity - ComponentRequest.issued_quantity),
                        0
                    )
                )
                .filter(
                    ComponentRequest.component_id == component.id,
                    ComponentRequest.id != component_request.id,
                    ComponentRequest.status.in_(["Pending Admin", "Approved", "Partially Issued"]),
                    ComponentRequest.approved_quantity > ComponentRequest.issued_quantity
                )
                .scalar()
            ) or 0
            available_quantity = max(0, component.quantity - reserved_quantity)

            if remaining <= 0:
                flash("There is no outstanding approved quantity to authorize.", "warning")
                return redirect(url_for("admin_component_approvals"))
            if remaining > available_quantity:
                flash(f"Cannot approve: only {available_quantity} component(s) remain available after other reservations.", "danger")
                return redirect(url_for("admin_component_approvals"))

            component_request.status = "Approved"
            try:
                db.session.commit()
                flash("Admin approval granted. Faculty may now proceed to the issuing step.", "success")
            except Exception:
                db.session.rollback()
                app.logger.exception("Admin approval failed for request %s", request_id)
                flash("Admin approval failed. No changes were saved.", "danger")

        elif action == "reject":
            component_request.status = "Rejected"
            try:
                db.session.commit()
                flash("Request rejected by Admin.", "warning")
            except Exception:
                db.session.rollback()
                app.logger.exception("Admin rejection failed for request %s", request_id)
                flash("Admin rejection failed. No changes were saved.", "danger")
        else:
            flash("Invalid action.", "danger")

        return redirect(url_for("admin_component_approvals"))

    pending_requests = ComponentRequest.query.filter_by(
        status="Pending Admin"
    ).order_by(ComponentRequest.request_date.asc()).all()
    request_rows = []
    for item in pending_requests:
        request_rows.append({
            "request": item,
            "student": db.session.get(User, item.student_id),
            "component": db.session.get(Component, item.component_id),
        })

    return render_template("admin_component_approvals.html", request_rows=request_rows)


# Student request and issue history
@app.route("/student/request-history")
def student_request_history():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "Student":
        return "Access denied: Students only.", 403

    requests_list = ComponentRequest.query.filter_by(
        student_id=session["user_id"]
    ).order_by(
        ComponentRequest.request_date.desc()
    ).all()

    request_rows = []

    for item in requests_list:
        request_rows.append({
            "request": item,
            "component": db.session.get(
                Component, item.component_id
            )
        })

    return render_template(
        "student_request_history.html",
        request_rows=request_rows
    )

@app.route("/faculty/issue-history")
def faculty_issue_history():
    if "user_id" not in session:
        return redirect(url_for("login"))

    if session.get("role") != "Faculty":
        return "Access denied: Faculty only.", 403

    # Use the same key as the Admin Faculty access route.
    setting = SystemSetting.query.filter_by(
        key="faculty_access_enabled"
    ).first()

    if setting is None or setting.value != "True":
        return "Faculty access is disabled by Admin.", 403

    # Show only transactions performed by this logged-in faculty account.
    transactions = IssueTransaction.query.filter_by(
        faculty_id=session["user_id"]
    ).order_by(IssueTransaction.issue_date.desc()).all()

    request_rows = []
    for transaction in transactions:
        request_item = db.session.get(ComponentRequest, transaction.request_id)
        student = db.session.get(User, request_item.student_id) if request_item else None
        request_rows.append({
            "transaction": transaction,
            "request": request_item,
            "student": student,
            "component": db.session.get(Component, transaction.component_id),
        })

    return render_template(
        "faculty_issue_history.html",
        request_rows=request_rows
    )
# Safe migration: add issuing-window columns if missing.
with app.app_context():
    columns = {
        column["name"]
        for column in inspect(db.engine).get_columns(
            "component_request"
        )
    }

    with db.engine.begin() as connection:
        if "issue_window_started_at" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE component_request "
                "ADD COLUMN issue_window_started_at DATETIME"
            )

        if "issue_window_expires_at" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE component_request "
                "ADD COLUMN issue_window_expires_at DATETIME"
            )

    # Existing databases also need newer columns on issue_transaction.
    # db.create_all() does not add columns to an existing table.
    transaction_columns = {
        column["name"]
        for column in inspect(db.engine).get_columns("issue_transaction")
    }

    with db.engine.begin() as connection:
        if "issue_window_started_at" not in transaction_columns:
            connection.exec_driver_sql(
                "ALTER TABLE issue_transaction "
                "ADD COLUMN issue_window_started_at DATETIME"
            )

        if "issue_window_expires_at" not in transaction_columns:
            connection.exec_driver_sql(
                "ALTER TABLE issue_transaction "
                "ADD COLUMN issue_window_expires_at DATETIME"
            )

    print("Issuing-window database migration checked.")

# -------------------- Automatic return email reminders --------------------
# The app checks the four reminder stages while Flask is running.
# Reminder times use the computer/server's local date and time.
RETURN_REMINDER_STAGES = {
    -1: (
        "one_day_before",
        "Component return reminder: due tomorrow",
        "The component listed below is due tomorrow. Please arrange its return.",
        False,
    ),
    0: (
        "due_today",
        "Component return due today",
        "The component listed below is due today. Please return it today.",
        True,
    ),
    2: (
        "two_days_overdue",
        "Overdue component: faculty follow-up required",
        "The component listed below has not been fully returned and is now 2 days overdue. "
        "Student: please return it urgently. Issuing faculty: please follow up with the student.",
        False,
    ),
    4: (
        "four_days_overdue",
        "IMPORTANT: component still not returned",
        "The component listed below is still not fully returned and is now 4 days overdue. "
        "Admin and issuing faculty: please take action. Student: please return the outstanding "
        "component immediately.",
        True,
    ),
}


def _return_reminder_recipients(request_item, issue_rows, include_admins):
    """Build unique student/faculty/admin recipient addresses for one reminder stage."""
    recipients = set()

    student = db.session.get(User, request_item.student_id)
    if student and student.is_active and student.email:
        recipients.add(student.email.strip().lower())

    for issue_row in issue_rows:
        faculty = db.session.get(User, issue_row.faculty_id)
        if faculty and faculty.is_active and faculty.email:
            recipients.add(faculty.email.strip().lower())

    if include_admins:
        admins = User.query.filter(
            db.func.lower(User.role) == "admin",
            User.is_active.is_(True),
            User.email.isnot(None),
            User.email != "",
        ).all()
        recipients.update(
            admin.email.strip().lower()
            for admin in admins
            if admin.email and admin.email.strip()
        )

    return sorted(recipients), student


def _get_request_outstanding_quantity(request_item):
    issue_rows = IssueTransaction.query.filter_by(request_id=request_item.id).all()
    total_issued = sum(row.quantity for row in issue_rows)
    total_returned = 0

    for issue_row in issue_rows:
        returned = (
            db.session.query(
                db.func.coalesce(db.func.sum(ReturnTransaction.quantity), 0)
            )
            .filter_by(issue_transaction_id=issue_row.id)
            .scalar()
            or 0
        )
        total_returned += returned

    return max(total_issued - total_returned, 0), issue_rows


def check_and_send_return_reminders():
    """Send any reminder stage due today; safe to call repeatedly."""
    with app.app_context():
        today = datetime.now().date()

        for day_offset, stage_info in RETURN_REMINDER_STAGES.items():
            stage, subject_text, message_text, include_admins = stage_info
            due_date = today - timedelta(days=day_offset)

            due_requests = ComponentRequest.query.filter(
                ComponentRequest.return_date == due_date,
                ComponentRequest.issued_quantity > 0,
            ).all()

            for request_item in due_requests:
                outstanding, issue_rows = _get_request_outstanding_quantity(request_item)
                if outstanding <= 0:
                    continue

                recipients, student = _return_reminder_recipients(
                    request_item, issue_rows, include_admins
                )
                component = db.session.get(Component, request_item.component_id)
                component_name = component.name if component else "Unknown component"

                body = (
                    f"{message_text}\\n\\n"
                    f"Request ID: {request_item.id}\\n"
                    f"Component: {component_name}\\n"
                    f"Student: {student.username if student else 'Unknown'}\\n"
                    f"Outstanding quantity: {outstanding}\\n"
                    f"Return due date: {request_item.return_date}\\n"
                )
                subject = f"{subject_text}: {component_name} (Request #{request_item.id})"

                for recipient in recipients:
                    already_sent = DueReminderRecipientLog.query.filter_by(
                        request_id=request_item.id,
                        stage=stage,
                        recipient_email=recipient,
                    ).first()
                    if already_sent:
                        continue

                    try:
                        if send_account_email(recipient, subject, body):
                            db.session.add(DueReminderRecipientLog(
                                request_id=request_item.id,
                                stage=stage,
                                recipient_email=recipient,
                                sent_at=datetime.utcnow(),
                            ))
                            db.session.commit()
                    except Exception:
                        db.session.rollback()
                        app.logger.exception(
                            "Return reminder '%s' failed for %s", stage, recipient
                        )


def _return_reminder_scheduler_loop():
    """Check every five minutes so temporary mail failures can be retried that day."""
    while True:
        try:
            check_and_send_return_reminders()
        except Exception:
            app.logger.exception("Automatic return reminder check failed")
        time.sleep(300)


def start_return_reminder_scheduler():
    """Start one background reminder thread in the active Flask process."""
    if getattr(app, "_return_reminder_scheduler_started", False):
        return

    app._return_reminder_scheduler_started = True
    thread = threading.Thread(
        target=_return_reminder_scheduler_loop,
        name="return-email-reminder-scheduler",
        daemon=True,
    )
    thread.start()


if __name__ == "__main__":
    # Werkzeug's debug reloader starts a parent and a child process.
    # Start the scheduler only in the child to avoid duplicate reminder emails.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_return_reminder_scheduler()

    app.run(debug=True)