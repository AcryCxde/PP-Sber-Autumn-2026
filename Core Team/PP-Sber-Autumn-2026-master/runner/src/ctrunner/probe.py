"""Проверка ключа cliproxy до старта SDK: отличить отозванный ключ от недоступной сети."""

import http.client
import json
import urllib.request
from enum import StrEnum
from typing import Final
from urllib.error import HTTPError

REJECTED_CODES: Final = frozenset({401, 403})


class ProbeResult(StrEnum):
    OK = "ok"
    REJECTED = "rejected"
    UNREACHABLE = "unreachable"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib переносит Authorization на новый хост при редиректе: ключ не должен уйти туда.
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


_OPENER: Final = urllib.request.build_opener(_NoRedirect)


def probe(base_url: str, token: str, model: str, *, timeout_s: float) -> ProbeResult:
    body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}
    request = urllib.request.Request(  # noqa: S310 — схема http(s) проверена в config
        f"{base_url}/v1/messages",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with _OPENER.open(request, timeout=timeout_s):
            return ProbeResult.OK
    except HTTPError as error:
        error.close()
        return ProbeResult.REJECTED if error.code in REJECTED_CODES else ProbeResult.UNREACHABLE
    except (OSError, http.client.HTTPException):
        return ProbeResult.UNREACHABLE
