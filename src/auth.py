import time

import requests
from keboola.component.exceptions import UserException


class TokenManager:
    def __init__(self, base_url, app_id, client_id, client_secret, scope="s3api", session=None):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.session = session or requests.Session()
        self._token = None
        self._expires_at = 0.0

    def get_token(self) -> str:
        if self._token and time.time() < self._expires_at - 30:
            return self._token
        url = f"{self.base_url}/connect/token"
        try:
            resp = self.session.post(
                url,
                params={"AppId": self.app_id},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": self.scope,
                },
                timeout=30,
            )
        except requests.RequestException as e:
            raise UserException(f"Cannot reach Money S3 token endpoint {url}: {e}") from e
        if resp.status_code != 200:
            raise UserException(f"Money S3 authentication failed ({resp.status_code}): {resp.text[:300]}")
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in") or 3600)
        return self._token
