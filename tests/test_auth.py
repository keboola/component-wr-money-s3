from unittest.mock import MagicMock

import pytest
from keboola.component.exceptions import UserException

from auth import TokenManager


def _resp(status, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.text = text
    return r


def test_fetches_and_caches_token():
    session = MagicMock()
    session.post.return_value = _resp(200, {"access_token": "tok", "expires_in": 3600})
    tm = TokenManager("http://h", "APP", "cid", "secret", session=session)
    assert tm.get_token() == "tok"
    assert tm.get_token() == "tok"
    assert session.post.call_count == 1  # cached
    _, kwargs = session.post.call_args
    assert kwargs["params"] == {"AppId": "APP"}
    assert kwargs["data"]["grant_type"] == "client_credentials"
    assert kwargs["data"]["scope"] == "s3api"


def test_missing_expires_in_defaults_and_does_not_crash():
    session = MagicMock()
    # A response with no/null expires_in must not raise and must still cache the token.
    session.post.return_value = _resp(200, {"access_token": "tok", "expires_in": None})
    tm = TokenManager("http://h", "APP", "cid", "secret", session=session)
    assert tm.get_token() == "tok"
    assert tm.get_token() == "tok"
    assert session.post.call_count == 1  # default TTL applied -> still cached


def test_auth_failure_raises_userexception():
    session = MagicMock()
    session.post.return_value = _resp(401, text="bad creds")
    tm = TokenManager("http://h", "APP", "cid", "secret", session=session)
    with pytest.raises(UserException):
        tm.get_token()
