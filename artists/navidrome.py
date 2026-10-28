import json
import logging
import uuid
from urllib.parse import urlparse

import requests

from cmn_artist.activity import redact

logger = logging.getLogger(__name__)

MAX_LOGGED_BODY = 2000


def _raise_for_status(resp: requests.Response) -> None:
    """Like resp.raise_for_status(), but includes the response body so the
    actual Navidrome error (e.g. duplicate username) shows up in logs."""
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise requests.HTTPError(f"{exc}: {resp.text}", response=resp) from exc


def _loggable_body(body) -> str:
    """JSON bodies are logged with passwords/tokens redacted; anything else as plain text."""
    if not body:
        return ''
    try:
        text = json.dumps(redact(json.loads(body)))
    except ValueError:
        text = body.decode('utf-8', 'replace') if isinstance(body, bytes) else str(body)
    return text[:MAX_LOGGED_BODY]


def _log_exchange(resp: requests.Response, *args, **kwargs) -> None:
    """requests response hook: log every call we make to Navidrome."""
    req = resp.request
    logger.info(
        'Navidrome %s %s -> %s request=%s response=%s',
        req.method, req.url, resp.status_code, _loggable_body(req.body), _loggable_body(resp.content),
    )


def _admin_session(base_url: str, admin_user: str, admin_pass: str):
    """Authenticate as admin and return (session, token)."""
    session = requests.Session()
    session.hooks['response'].append(_log_exchange)
    resp = session.post(base_url.rstrip("/") + "/auth/login",
                        json={"username": admin_user, "password": admin_pass})
    _raise_for_status(resp)
    data = resp.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"Navidrome login response missing token: {data}")

    client_id = str(uuid.uuid4())
    try:
        domain = urlparse(base_url).hostname
        session.cookies.set("X-ND-Client-Unique-Id", client_id,
                            domain=domain if domain else None, path="/")
    except Exception:
        session.cookies.set("X-ND-Client-Unique-Id", client_id, path="/")

    session.headers.update({
        "Authorization": f"Bearer {token}",
        "x-nd-authorization": f"Bearer {token}",
        "x-nd-client-unique-id": client_id,
        "Accept": "application/json",
    })
    return session


def create_navidrome_user(base_url: str, admin_user: str, admin_pass: str,
                          username: str, display_name: str, email: str, password: str) -> str:
    """
    Log in to Navidrome as admin and create a new user.
    Returns the Navidrome user ID. Raises on failure.
    """
    session = _admin_session(base_url, admin_user, admin_pass)
    payload = {
        "userName": username,
        "name": display_name,
        "email": email,
        "password": password,
        "isAdmin": False,
    }
    resp = session.post(
        base_url.rstrip("/") + "/api/user",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(resp)
    return resp.json()["id"]


def update_navidrome_password(base_url: str, admin_user: str, admin_pass: str,
                              nd_user_id: str, new_password: str) -> None:
    """Update a Navidrome user's password via the admin API.

    Navidrome's PUT replaces the whole user record, so a password-only payload
    would blank userName/name/email. Fetch the current record and send it back
    with the new password."""
    session = _admin_session(base_url, admin_user, admin_pass)
    url = base_url.rstrip("/") + f"/api/user/{nd_user_id}"
    resp = session.get(url)
    _raise_for_status(resp)
    user = resp.json()
    user["password"] = new_password
    resp = session.put(
        url,
        json=user,
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(resp)


def grant_library_access(base_url: str, admin_user: str, admin_pass: str,
                         nd_user_id: str, library_id: str, sample_library_id: str = None) -> None:
    """Grant a Navidrome user access to the given library, keeping the sample library too."""
    library_ids = [library_id]
    if sample_library_id is not None:
        library_ids.append(sample_library_id)
    session = _admin_session(base_url, admin_user, admin_pass)
    resp = session.put(
        base_url.rstrip("/") + f"/api/user/{nd_user_id}/library",
        json={"libraryIds": library_ids},
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(resp)


def revoke_library_access(base_url: str, admin_user: str, admin_pass: str,
                          nd_user_id: str, sample_library_id: str = None) -> None:
    """Remove paid library access for a Navidrome user, keeping the sample library."""
    library_ids = [sample_library_id] if sample_library_id is not None else []
    session = _admin_session(base_url, admin_user, admin_pass)
    resp = session.put(
        base_url.rstrip("/") + f"/api/user/{nd_user_id}/library",
        json={"libraryIds": library_ids},
        headers={"Content-Type": "application/json"},
    )
    _raise_for_status(resp)
