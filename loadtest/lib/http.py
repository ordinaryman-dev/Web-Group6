# HTTP 客户端：每个虚拟用户独立 Session；真实走登录表单、CSRF、搜索、确认页和提交预订。
"""A small HTTP flow layer for the guest portal.

Every virtual user owns one :class:`requests.Session` and performs the same
steps a browser does: GET the login page, submit the CSRF-protected form,
search for a stay, open the confirmation page and post the booking form.
"""

import html
import re
import time

import requests


_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_FLASH_RE = re.compile(
    r'<p class="flash\s+([^"]+)"[^>]*>(.*?)</p>', re.IGNORECASE | re.DOTALL
)
_DETAIL_RE = re.compile(r"/guest/bookings/(\d+)")


class FlowError(RuntimeError):
    """The browser-level flow could not be completed (login, CSRF, page shape)."""


def hidden_fields(page):
    """Collect every ``type=hidden`` input from a rendered form."""

    fields = {}
    for tag in _INPUT_RE.findall(page):
        attrs = {key.lower(): html.unescape(value) for key, value in _ATTR_RE.findall(tag)}
        if attrs.get("type", "").lower() == "hidden" and attrs.get("name"):
            fields[attrs["name"]] = attrs.get("value", "")
    return fields


def flash_messages(page):
    messages = []
    for category, body in _FLASH_RE.findall(page):
        text = re.sub(r"<[^>]+>", " ", body)
        messages.append((category.strip(), html.unescape(re.sub(r"\s+", " ", text)).strip()))
    return messages


def booking_id_from_url(url):
    match = _DETAIL_RE.search(url)
    return int(match.group(1)) if match else None


class GuestClient:
    """One logged-in guest browser session."""

    def __init__(self, email, password, base_url, timeout=30):
        self.email = email
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "hero-loadtest/1.0 (COMP3019J)",
            "Accept-Language": "en",
        })

    # -- authentication -------------------------------------------------
    def login(self):
        page = self.session.get(f"{self.base_url}/login", timeout=self.timeout)
        page.raise_for_status()
        token = hidden_fields(page.text).get("csrf_token")
        if not token:
            raise FlowError(f"login page for {self.email} has no CSRF token")
        response = self.session.post(
            f"{self.base_url}/login",
            data={
                "csrf_token": token,
                "email": self.email,
                "password": self.password,
                "submit": "Sign in",
            },
            timeout=self.timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
        if "/guest" not in response.url:
            messages = flash_messages(response.text)
            raise FlowError(f"login failed for {self.email}: {response.url} {messages}")
        return response

    # -- browsing -------------------------------------------------------
    def search_page(self, check_in, check_out, guests=1, bed_type="all"):
        return self.session.get(
            f"{self.base_url}/guest/",
            params={
                "check_in": check_in,
                "check_out": check_out,
                "guests": guests,
                "bed_type": bed_type,
            },
            timeout=self.timeout,
        )

    def search_api(self, check_in, check_out, guests=1):
        return self.session.get(
            f"{self.base_url}/api/search",
            params={"check_in": check_in, "check_out": check_out, "guests": guests},
            timeout=self.timeout,
        )

    def bookings_page(self):
        return self.session.get(f"{self.base_url}/guest/bookings", timeout=self.timeout)

    def status_feed(self):
        return self.session.get(
            f"{self.base_url}/guest/status-feed", timeout=self.timeout
        )

    def booking_detail(self, booking_id):
        return self.session.get(
            f"{self.base_url}/guest/bookings/{booking_id}", timeout=self.timeout
        )

    # -- booking --------------------------------------------------------
    def prepare_booking(self, room_type_id, check_in, check_out, guests=1):
        """Open the confirmation page and return the hidden form payload."""

        started = time.perf_counter()
        response = self.session.get(
            f"{self.base_url}/guest/book/{room_type_id}",
            params={
                "check_in": check_in,
                "check_out": check_out,
                "guests": guests,
            },
            timeout=self.timeout,
            allow_redirects=True,
        )
        elapsed = (time.perf_counter() - started) * 1000
        fields = hidden_fields(response.text)
        if response.status_code != 200 or "/guest/book/" not in response.url:
            return None, elapsed, flash_messages(response.text)
        if "csrf_token" not in fields or "quote" not in fields:
            return None, elapsed, [("error", "confirmation page has no booking form")]
        fields.update({
            "guests": str(guests),
            "expected_arrival": "15:00",
            "note": "load test",
            "submit": "Confirm booking",
        })
        return fields, elapsed, []

    def submit_booking(self, fields):
        """Post the confirmation form; return latency and a classified outcome."""

        started = time.perf_counter()
        response = self.session.post(
            f"{self.base_url}/guest/book",
            data=fields,
            timeout=self.timeout,
            allow_redirects=True,
        )
        elapsed = (time.perf_counter() - started) * 1000
        booking_id = booking_id_from_url(response.url)
        messages = flash_messages(response.text)
        text = response.text.casefold()
        if response.status_code >= 500:
            outcome = "http_error"
        elif booking_id is not None:
            outcome = "success"
        elif "rooms are busy" in text:
            outcome = "busy"
        elif "no single room covers" in text or "room type is unavailable" in text:
            outcome = "sold_out"
        elif "price changed" in text:
            outcome = "price_changed"
        elif "belongs to a different booking" in text:
            outcome = "idempotency_mismatch"
        elif response.status_code == 400:
            outcome = "invalid"
        elif messages:
            outcome = "rejected"
        else:
            outcome = "unknown"
        detail = "; ".join(message for _, message in messages)[:240]
        return {
            "status_code": response.status_code,
            "latency_ms": elapsed,
            "outcome": outcome,
            "booking_id": booking_id,
            "detail": detail,
            "url": response.url,
        }


def public_api_client(base_url, timeout=30):
    """Anonymous session used by the browse scenario (no login required)."""

    session = requests.Session()
    session.headers.update({
        "User-Agent": "hero-loadtest/1.0 (COMP3019J)",
        "Accept-Language": "en",
    })

    class PublicClient:
        def search_api(self, check_in, check_out, guests=1):
            return session.get(
                f"{base_url}/api/search",
                params={"check_in": check_in, "check_out": check_out, "guests": guests},
                timeout=timeout,
            )

        def search_page(self, check_in, check_out, guests=1, bed_type="all"):
            return session.get(
                f"{base_url}/guest/",
                params={
                    "check_in": check_in,
                    "check_out": check_out,
                    "guests": guests,
                    "bed_type": bed_type,
                },
                timeout=timeout,
            )

    return PublicClient()
