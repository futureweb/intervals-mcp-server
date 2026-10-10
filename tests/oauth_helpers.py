"""Helpers shared by the OAuth tests: submit the consent form like a browser does."""

import re
from typing import Any

from starlette.testclient import TestClient

_CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')


def consent_token(client: TestClient, request_id: str) -> str:
    """Open the consent page (sets the consent cookie) and return its form token."""
    page = client.get("/oauth/login", params={"request": request_id})
    match = _CSRF_FIELD.search(page.text)
    return match.group(1) if match else ""


def submit_consent(client: TestClient, data: dict[str, Any], **kwargs: Any) -> Any:
    """POST the consent form after loading the page, as the athlete's browser would."""
    form = dict(data)
    form.setdefault("csrf", consent_token(client, str(form.get("request", ""))))
    return client.post("/oauth/login", data=form, **kwargs)
