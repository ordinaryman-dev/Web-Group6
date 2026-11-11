from sqlalchemy import select

from app.extensions import db
from app.models import AuditLog, Customer, User

from .helpers import PASSWORD, post_form, sign_in


def test_register_login_change_password_and_logout(app, client):
    data = {"name": "Sam Guest", "email": " Sam@Example.test ", "phone": "123456", "password": PASSWORD}
    response = post_form(client, "/register", {**data, "role": "manager"})
    assert response.status_code == 302
    with app.app_context():
        user = db.session.scalar(select(User).where(User.email == "sam@example.test"))
        customer = db.session.scalar(select(Customer).where(Customer.user_id == user.id))
        assert user.role == "guest"
        assert user.password_hash != PASSWORD
        assert customer.name == "Sam Guest"
        assert db.session.scalar(select(AuditLog.action)) == "register"

    assert post_form(client, "/register", data).status_code == 409
    assert sign_in(client, "SAM@example.test").status_code == 302
    assert b"Guest portal" in client.get("/portal").data
    assert b"Sam Guest" in client.get("/profile").data
    assert client.get("/manager/staff").status_code == 403

    new_password = "replacement-password"
    change = {"current_password": PASSWORD, "new_password": new_password}
    assert post_form(client, "/profile/password", {**change, "current_password": "incorrect"}).status_code == 400
    assert post_form(client, "/profile/password", change).status_code == 302
    assert post_form(client, "/logout", {}, token_page="/profile").status_code == 302
    assert sign_in(client, "sam@example.test").status_code == 401
    assert sign_in(client, "sam@example.test", new_password).status_code == 302


def test_manager_can_create_and_disable_staff(app, client):
    email = "manager@example.test"
    result = app.test_cli_runner().invoke(args=["bootstrap-manager", email, "--password", PASSWORD])
    assert result.exit_code == 0, result.output
    assert sign_in(client, email).status_code == 302
    assert b"Manager console" in client.get("/portal").data
    staff_email = "desk@example.test"
    assert post_form(client, "/manager/staff/new", {"email": staff_email, "password": PASSWORD}).status_code == 302
    assert staff_email.encode() in client.get("/manager/staff").data
    staff_client = app.test_client()
    assert sign_in(staff_client, staff_email).status_code == 302
    assert b"Front desk" in staff_client.get("/portal").data
    assert staff_client.get("/manager/staff").status_code == 403
    assert post_form(staff_client, "/manager/staff/new", {
        "email": "other@example.test", "password": PASSWORD,
    }, token_page="/portal").status_code == 403

    with app.app_context():
        staff_id = db.session.scalar(select(User.id).where(User.email == staff_email))
    assert post_form(client, f"/manager/staff/{staff_id}/disable", {}, token_page="/manager/staff").status_code == 302
    assert staff_client.get("/portal").status_code == 302
    assert sign_in(app.test_client(), staff_email).status_code == 401
    with app.app_context():
        assert db.session.get(User, staff_id).active is False
        audit = db.session.scalar(select(AuditLog).where(AuditLog.action == "disable_staff"))
        assert audit.object_id == staff_id
        assert audit.details == {"previous_active": True, "active": False}


def test_authenticated_changes_require_csrf(app, client):
    assert client.get("/profile").status_code == 302
    assert client.post("/register", data={
        "name": "Guest", "email": "guest@example.test", "password": PASSWORD,
    }).status_code == 400
    assert client.get("/logout").status_code == 405
    with app.app_context():
        assert db.session.scalar(select(User.id)) is None
