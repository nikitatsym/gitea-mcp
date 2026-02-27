from __future__ import annotations

import os

import httpx

_DEFAULT_LIMIT = 50


class GiteaError(Exception):
    def __init__(self, status: int, method: str, path: str, body):
        self.status = status
        self.method = method
        self.path = path
        self.body = body
        super().__init__(f"Gitea API {status} {method} {path}: {body}")


class GiteaClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        self._base = (base_url or os.environ["GITEA_URL"]).rstrip("/")
        self._token = token or os.environ["GITEA_TOKEN"]
        self._http = httpx.Client(
            base_url=f"{self._base}/api/v1",
            headers={"Authorization": f"token {self._token}"},
            timeout=30.0,
        )

    # ── low-level ────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        r = self._http.request(method, path, **kwargs)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise GiteaError(r.status_code, method, path, body)
        return r

    def _json(self, method: str, path: str, **kwargs):
        r = self._request(method, path, **kwargs)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _text(self, method: str, path: str, **kwargs) -> str:
        r = self._request(method, path, **kwargs)
        return r.text

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

    def paginate(self, path: str, params: dict | None = None) -> list:
        return self._paginate(path, params)
