"""KubeArchive REST client for archived Tekton resources."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class KubeArchiveAuthError(Exception):
    """Raised when KubeArchive rejects the bearer token (HTTP 401 / 403)."""

    def __init__(self, code: int, path: str) -> None:
        self.code = code
        self.path = path
        super().__init__(
            f"KubeArchive HTTP {code} for {path!r}: token may be expired or lack permission "
            "to read archived Tekton resources."
        )


def _ka_ts() -> str:
    return time.strftime("%H:%M:%S")


def _http_timeout_s() -> float:
    """Per-request timeout for KubeArchive GET (connect + read body).

    Default 20s; override with ``OLMINSTALL_KA_HTTP_TIMEOUT`` (5–120).
    """
    raw = os.environ.get("OLMINSTALL_KA_HTTP_TIMEOUT", "20").strip()
    try:
        v = float(raw)
    except ValueError:
        v = 20.0
    return max(5.0, min(v, 120.0))


class KubeArchiveClient:
    def __init__(self, host: str, token: str) -> None:
        self.host = host.rstrip("/")
        parsed = urlparse(self.host)
        if parsed.scheme != "https":
            raise ValueError(f"Unsupported KubeArchive host scheme: {parsed.scheme!r}")
        if not parsed.netloc:
            raise ValueError("KubeArchive host must include a valid hostname")
        self.host = f"https://{parsed.netloc}{parsed.path}".rstrip("/")
        self.token = token
        self.available: bool | None = None

    def _request(self, path: str) -> str:
        req = Request(
            f"{self.host}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        t0 = time.monotonic()
        try:
            with urlopen(req, timeout=_http_timeout_s()) as resp:
                return resp.read().decode("utf-8")
        except HTTPError as exc:
            elapsed = time.monotonic() - t0
            if exc.code in (401, 403):
                raise KubeArchiveAuthError(exc.code, path) from exc
            print(
                f"[{_ka_ts()}] WARN KubeArchive HTTP {exc.code} for GET {path} (after {elapsed:.1f}s)",
                file=sys.stderr,
            )
            return ""
        except URLError as exc:
            elapsed = time.monotonic() - t0
            reason = getattr(exc, "reason", exc)
            print(
                f"[{_ka_ts()}] WARN KubeArchive request failed for GET {path} (after {elapsed:.1f}s): {reason}",
                file=sys.stderr,
            )
            return ""
        except OSError as exc:
            # Catches IncompleteRead, RemoteDisconnected, and other socket/chunked-transfer errors
            # that are not wrapped by urllib into URLError.
            elapsed = time.monotonic() - t0
            print(
                f"[{_ka_ts()}] WARN KubeArchive read error for GET {path} (after {elapsed:.1f}s): {exc}",
                file=sys.stderr,
            )
            return ""

    def check(self) -> bool:
        """Return whether KubeArchive ``/livez`` responds OK.

        On HTTP 401/403 returns ``False`` (warns) instead of raising; see
        :meth:`get_json` / :meth:`get_text` which may still raise
        :class:`KubeArchiveAuthError` for protected paths when the token is invalid.
        """
        if self.available is None:
            try:
                raw = self._request("/livez")
            except KubeArchiveAuthError:
                print(
                    f"[{_ka_ts()}] WARN KubeArchive auth failed for /livez; treating as unavailable.",
                    file=sys.stderr,
                )
                self.available = False
                return False
            try:
                self.available = bool(raw and json.loads(raw).get("code") == 200)
            except json.JSONDecodeError:
                self.available = False
        return bool(self.available)

    def get_json(self, path: str) -> dict[str, Any]:
        """GET *path* and parse JSON. May raise :class:`KubeArchiveAuthError` (401/403)."""
        raw = self._request(path)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def get_text(self, path: str) -> str:
        """GET *path* and return body text. May raise :class:`KubeArchiveAuthError` (401/403)."""
        return self._request(path)
