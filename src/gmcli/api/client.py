"""The authenticated Gmail service, plus retry and batching.

Two things matter for a CLI here. First, transient failures (429, 5xx) must not
surface as tracebacks — they are retried with backoff. Second, ``messages.list``
returns bare ids, so rendering a 20-row table naively costs 21 round trips;
:meth:`GmailClient.batch_get` folds a page of metadata fetches into one HTTP
request instead.
"""

from __future__ import annotations

import random
import socket
import ssl
import time
from typing import Any, Callable, Iterable, Sequence

from ..errors import ApiError, AuthError, NotFoundError

# Gmail allows 100 sub-requests per batch but recommends staying well under it;
# 50 keeps individual requests small enough to retry cheaply.
BATCH_CHUNK = 50

MAX_ATTEMPTS = 5
BASE_DELAY = 0.5
MAX_DELAY = 16.0

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 403 is overloaded: quota errors are retryable, permission errors are not.
RETRYABLE_403_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
    "backendError",
}
PERMISSION_403_REASONS = {
    "insufficientPermissions",
    "forbidden",
    "accessNotConfigured",
}

_TRANSIENT_EXCEPTIONS = (socket.timeout, ssl.SSLError, ConnectionError, OSError)


def _status_of(error: Any) -> int | None:
    resp = getattr(error, "resp", None)
    status = getattr(resp, "status", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            return None
    return getattr(error, "status_code", None)


def _reason_of(error: Any) -> str:
    """Pull the machine-readable reason out of an HttpError body."""
    import json

    content = getattr(error, "content", None)
    if not content:
        return ""
    try:
        body = json.loads(content.decode() if isinstance(content, bytes) else content)
        errors = body.get("error", {}).get("errors") or []
        if errors:
            return errors[0].get("reason", "")
        return body.get("error", {}).get("status", "")
    except (ValueError, AttributeError):
        return ""


def _message_of(error: Any) -> str:
    import json

    content = getattr(error, "content", None)
    if content:
        try:
            body = json.loads(
                content.decode() if isinstance(content, bytes) else content
            )
            msg = body.get("error", {}).get("message")
            if msg:
                return msg
        except (ValueError, AttributeError):
            pass
    return str(error)


def _retry_after(error: Any) -> float | None:
    resp = getattr(error, "resp", None)
    if resp is None:
        return None
    raw = None
    try:
        raw = resp.get("retry-after") or resp.get("Retry-After")
    except AttributeError:
        raw = getattr(resp, "retry_after", None)
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def translate_error(error: Exception) -> Exception:
    """Map a Google API exception onto gmcli's error hierarchy."""
    from googleapiclient.errors import HttpError

    if isinstance(error, HttpError):
        status = _status_of(error)
        reason = _reason_of(error)
        detail = _message_of(error)
        if status == 404:
            return NotFoundError(f"Not found: {detail}")
        if status == 401:
            return AuthError(
                f"Gmail rejected the credentials: {detail}",
                hint="Run `gmail auth login` to re-authorize.",
            )
        if status == 403 and reason in PERMISSION_403_REASONS:
            return AuthError(
                f"Permission denied: {detail}",
                hint="If you recently changed scopes, run `gmail auth login` "
                "again to re-consent. Note gmcli never requests permanent-delete "
                "permission by design.",
            )
        if status == 400:
            return ApiError(f"Gmail rejected the request: {detail}")
        return ApiError(f"Gmail API error ({status}): {detail}")
    return ApiError(f"{type(error).__name__}: {error}")


class GmailClient:
    """Authenticated Gmail service with retry, batching, and error mapping."""

    def __init__(self, service: Any, *, user_id: str = "me") -> None:
        self.service = service
        self.user_id = user_id

    # -- construction --------------------------------------------------------

    @classmethod
    def for_account(cls, account: str) -> "GmailClient":
        from googleapiclient.discovery import build

        from ..auth.flow import load_credentials

        creds = load_credentials(account)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return cls(service)

    # -- single requests -----------------------------------------------------

    def execute(self, request: Any) -> Any:
        """Execute one API request, retrying transient failures with backoff."""
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return request.execute(num_retries=0)
            except RefreshError as exc:
                # google-auth already tried to refresh; a failure here is final.
                raise AuthError(
                    f"Could not refresh credentials: {exc}",
                    hint="Run `gmail auth login` to re-authorize.",
                ) from exc
            except HttpError as exc:
                status = _status_of(exc)
                reason = _reason_of(exc)
                retryable = status in RETRYABLE_STATUS or (
                    status == 403 and reason in RETRYABLE_403_REASONS
                )
                if not retryable or attempt == MAX_ATTEMPTS - 1:
                    raise translate_error(exc) from exc
                last = exc
                self._sleep(attempt, _retry_after(exc))
            except _TRANSIENT_EXCEPTIONS as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ApiError(
                        f"Network error talking to Gmail: {exc}",
                        hint="Check connectivity and try again.",
                    ) from exc
                last = exc
                self._sleep(attempt, None)

        raise translate_error(last) if last else ApiError("Request failed.")

    @staticmethod
    def _sleep(attempt: int, retry_after: float | None) -> None:
        """Exponential backoff with jitter, honoring Retry-After when present."""
        if retry_after is not None:
            delay = min(retry_after, MAX_DELAY)
        else:
            delay = min(BASE_DELAY * (2**attempt), MAX_DELAY)
        time.sleep(delay + random.uniform(0, delay * 0.25))

    # -- batched requests ----------------------------------------------------

    def batch_get(
        self,
        build_request: Callable[[str], Any],
        ids: Sequence[str],
        *,
        chunk_size: int = BATCH_CHUNK,
    ) -> dict[str, Any]:
        """Fetch many resources in as few HTTP requests as possible.

        ``build_request(id)`` returns the per-item API request. Results come
        back keyed by id; items that failed are simply absent, so a single
        deleted message in a page cannot fail the whole listing.
        """
        if not ids:
            return {}

        results: dict[str, Any] = {}
        errors: list[tuple[str, Exception]] = []

        for chunk in _chunks(list(ids), chunk_size):
            batch = self.service.new_batch_http_request()

            def callback(request_id: str, response: Any, exception: Any) -> None:
                if exception is not None:
                    errors.append((request_id, exception))
                else:
                    results[request_id] = response

            for item_id in chunk:
                batch.add(build_request(item_id), request_id=item_id, callback=callback)

            self._execute_batch(batch)

        if errors and not results:
            # Every item failed — surface the first cause rather than an
            # empty listing that looks like "no mail".
            raise translate_error(errors[0][1])
        return results

    def _execute_batch(self, batch: Any) -> None:
        from googleapiclient.errors import HttpError

        for attempt in range(MAX_ATTEMPTS):
            try:
                batch.execute()
                return
            except HttpError as exc:
                status = _status_of(exc)
                if status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS - 1:
                    raise translate_error(exc) from exc
                self._sleep(attempt, _retry_after(exc))
            except _TRANSIENT_EXCEPTIONS as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise ApiError(f"Network error talking to Gmail: {exc}") from exc
                self._sleep(attempt, None)

    # -- pagination ----------------------------------------------------------

    def paginate(
        self,
        method: Any,
        *,
        limit: int | None,
        items_key: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Follow nextPageToken until ``limit`` items are collected.

        Gmail caps maxResults at 500 per page; we also never ask for more than
        we still need, so ``-n 5`` costs one small request.
        """
        collected: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            params = dict(kwargs)
            if limit is not None:
                remaining = limit - len(collected)
                if remaining <= 0:
                    break
                params["maxResults"] = min(remaining, 500)
            if page_token:
                params["pageToken"] = page_token

            response = self.execute(method(userId=self.user_id, **params))
            collected.extend(response.get(items_key, []) or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return collected[:limit] if limit is not None else collected


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
