from html.parser import HTMLParser

from werkzeug.security import generate_password_hash

from app.extensions import db
from app.models import Customer, GUEST, User


PASSWORD = "first-test-password"


class TokenParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.token = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and values.get("name") == "csrf_token":
            self.token = values["value"]


def post_form(client, path, data, *, token_page=None, **kwargs):
    page = client.get(token_page or path)
    parser = TokenParser()
    parser.feed(page.get_data(as_text=True))
    assert parser.token is not None, page.status_code
    return client.post(path, data={**data, "csrf_token": parser.token}, **kwargs)


def sign_in(client, email, password=PASSWORD):
    return post_form(client, "/login", {"email": email, "password": password})


def make_guest(app, email):
    with app.app_context():
        user = User(email=email, role=GUEST, password_hash=generate_password_hash(PASSWORD))
        db.session.add(Customer(user=user, name="Test guest"))
        db.session.commit()
        return user.id
