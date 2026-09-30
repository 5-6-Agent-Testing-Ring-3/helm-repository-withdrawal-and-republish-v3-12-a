from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Any, cast

from chartpub.errors import PublicationError, RemoteConflict


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        if not newurl.startswith("https://"):
            raise PublicationError("refusing insecure redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            redirected
            and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc
        ):
            redirected.remove_header("Authorization")
        return redirected


def open_url(request: urllib.request.Request, timeout: int) -> HTTPResponse:
    return cast(
        HTTPResponse, urllib.request.build_opener(SafeRedirect()).open(request, timeout=timeout)
    )


@dataclass(frozen=True)
class Response:
    status: int
    body: Any
    headers: dict[str, str]


class GitHubClient:
    def __init__(
        self, repository: str, token: str, api_url: str = "https://api.github.com"
    ) -> None:
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.base = f"/repos/{repository}"

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        data: bytes | None = None,
        binary: bool = False,
        content_type: str = "application/vnd.github+json",
        etag: str | None = None,
    ) -> Response:
        url = path if path.startswith("https://") else self.api_url + path
        if urllib.parse.urlsplit(url).netloc not in {"api.github.com", "uploads.github.com"}:
            raise PublicationError("refusing credential delivery to unexpected host")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type,
            "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if etag:
            headers["If-Match"] = etag
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else data,
            method=method,
            headers=headers,
        )
        try:
            with open_url(request, timeout=60) as response:
                body = response.read()
                return Response(
                    response.status,
                    body if binary else json.loads(body) if body else None,
                    {k.lower(): v for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            # Server bodies and URLs are intentionally omitted: they can echo credentials.
            if exc.code == 404 and method == "GET":
                return Response(404, None, {})
            if exc.code in (409, 412, 422):
                raise RemoteConflict(f"GitHub {method} conflict ({exc.code})") from None
            raise PublicationError(f"GitHub {method} failed ({exc.code})") from None
        except (OSError, ValueError):
            raise PublicationError(f"GitHub {method} transport/response failure") from None

    def paginate(self, path: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            response = self.request("GET", f"{path}?per_page=100&page={page}")
            if response.status == 404:
                raise PublicationError("collection not found")
            result.extend(response.body)
            if len(response.body) < 100:
                return result
            page += 1

    def list_releases(self) -> list[dict[str, Any]]:
        releases = self.paginate(self.base + "/releases")
        for release in releases:
            release["assets"] = self.paginate(self.base + f"/releases/{release['id']}/assets")
        return releases

    def get_ref(self, ref: str) -> str | None:
        response = self.request("GET", self.base + "/git/ref/" + urllib.parse.quote(ref, safe="/"))
        return None if response.status == 404 else str(response.body["object"]["sha"])

    def release(self, release_id: int) -> Response:
        response = self.request("GET", self.base + f"/releases/{release_id}")
        if response.status == 404:
            raise RemoteConflict("release disappeared")
        return response

    def patch_release(self, expected: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        current = self.release(expected["id"])
        if fingerprint(current.body) != fingerprint(expected):
            raise RemoteConflict(f"release {expected['id']} changed")
        result = self.request(
            "PATCH",
            self.base + f"/releases/{expected['id']}",
            payload=payload,
        )
        # GitHub rejects conditional headers on release PATCH. Restrict the update
        # to the intended fields and verify the full result immediately afterward.
        intended = fingerprint(current.body | payload)
        actual = fingerprint(result.body)
        intended.pop("updated_at", None)
        actual.pop("updated_at", None)
        if actual != intended:
            raise RemoteConflict(
                f"release {expected['id']} changed during update; reconcile journal"
            )
        return dict(result.body)

    def public_bytes(self, url: str) -> bytes:
        try:
            with open_url(urllib.request.Request(url), timeout=60) as response:
                return response.read()
        except OSError:
            raise PublicationError("public Pages download failed") from None

    def download(self, asset_id: int) -> bytes:
        response = self.request("GET", self.base + f"/releases/assets/{asset_id}", binary=True)
        if response.status == 404 or not isinstance(response.body, bytes):
            raise PublicationError("asset download failed")
        return response.body


def fingerprint(release: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: release.get(key)
        for key in ("id", "tag_name", "target_commitish", "draft", "name", "body", "updated_at")
    }
    result["assets"] = sorted(
        [
            {key: asset.get(key) for key in ("id", "name", "size", "digest", "state")}
            for asset in release.get("assets", [])
        ],
        key=lambda asset: asset["id"],
    )
    return result
