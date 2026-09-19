from __future__ import annotations

import logging
import time

import httpx

from .config import Settings, get_settings

_DEFAULT_LIMIT = 50

_log = logging.getLogger("gitea_mcp.client")


class GiteaError(Exception):
    def __init__(self, status: int, method: str, path: str, body):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"Gitea API {status} {method} {path}: {body}")


class GiteaClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        settings: Settings | None = None,
    ):
        s = settings or get_settings()
        self._base = (base_url or s.gitea_url).rstrip("/")
        self._token = token or s.gitea_token
        self._http = httpx.Client(
            base_url=f"{self._base}/api/v1",
            headers={"Authorization": f"token {self._token}"},
            timeout=30.0,
            transport=transport,
        )

    def check(self) -> dict:
        """Verify the credential; returns what the version tool reports as `service`.

        Gitea serves /version anonymously but rejects an Authorization header it
        cannot resolve, so this catches a bad token without requiring a token
        scope the rest of the session may not need.
        """
        if not self._base or not self._token:
            raise ValueError("GITEA_URL and GITEA_TOKEN must be set")
        return self.get("/version")

    # ── low-level ────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        start = time.perf_counter()
        r = self._http.request(method, path, **kwargs)
        duration_ms = int((time.perf_counter() - start) * 1000)
        status = r.status_code
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO
        _log.log(level, "%s %s %d %dms", method, path, status, duration_ms)
        if status >= 400:
            try:
                body = r.json()
            except ValueError:
                # no-report: parse fallback for a non-JSON error body; the HTTP error raises below
                body = r.text
            raise GiteaError(status, method, path, body)
        return r

    def _json(self, method: str, path: str, **kwargs):
        r = self._request(method, path, **kwargs)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _text(self, method: str, path: str, **kwargs) -> str:
        r = self._request(method, path, **kwargs)
        return r.text

    def _bytes(self, method: str, path: str, **kwargs) -> bytes:
        r = self._request(method, path, **kwargs)
        return r.content

    def _paginate(self, path: str, params: dict | None = None, limit: int = _DEFAULT_LIMIT) -> list:
        params = dict(params or {})
        params["limit"] = limit
        page = 1
        result = []
        while True:
            params["page"] = page
            data = self._json("GET", path, params=params)
            if not data:
                break
            result.extend(data)
            if len(data) < limit:
                break
            page += 1
        return result

    # ── convenience HTTP verbs ───────────────────────────────

    def get(self, path: str, params: dict | None = None):
        return self._json("GET", path, params=params)

    def post(self, path: str, json=None, **kwargs):
        return self._json("POST", path, json=json, **kwargs)

    def put(self, path: str, json=None, **kwargs):
        return self._json("PUT", path, json=json, **kwargs)

    def patch(self, path: str, json=None, **kwargs):
        return self._json("PATCH", path, json=json, **kwargs)

    def delete(self, path: str, params: dict | None = None):
        return self._json("DELETE", path, params=params)

    def get_text(self, path: str, params: dict | None = None) -> str:
        return self._text("GET", path, params=params)

    def get_bytes(self, path: str, params: dict | None = None) -> bytes:
        """Raw response body, for endpoints that produce binary (media files)."""
        return self._bytes("GET", path, params=params)

    def download(self, path: str, params: dict | None = None) -> bytes:
        """GET a file the API serves via redirect, returning the bytes it lands on.

        Redirects are off everywhere else on purpose: httpx strips the
        Authorization header on a cross-origin hop, so a silently
        unauthenticated retry would look like an empty result. The endpoints
        that need this hand out a short-lived signed URL whose signature IS the
        credential, which is why dropping the header there is correct.
        """
        return self._bytes("GET", path, params=params, follow_redirects=True)

    def post_text(self, path: str, content: str, content_type: str) -> str:
        """POST a raw (non-JSON) request body; used by the markdown/markup renderers."""
        return self._text(
            "POST", path, content=content.encode(), headers={"Content-Type": content_type}
        )

    def upload(self, path: str, field: str, filename: str, data: bytes, params: dict | None = None):
        """POST one `multipart/form-data` file part.

        Gitea's attachment endpoints take the file under a fixed field name
        (`attachment`) and read the display name from a query param, not from
        the part. httpx sets the multipart Content-Type and boundary itself.
        """
        return self._json("POST", path, files={field: (filename, data)}, params=params)

    def paginate(self, path: str, params: dict | None = None) -> list:
        return self._paginate(path, params)
