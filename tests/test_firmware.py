"""The vendor firmware-release client.

The integration's only non-local call, and the only place a third party can
change what we see without shipping anything. So what is pinned here is mostly
about *not* trusting it: a 200 carrying an error envelope, a body that is not
JSON at all, an unreachable host, and the fallback that must fire for one of
those and not for the others.

The response shapes are the real ones, recorded from `dfu1.smartandgreen.eu` on
2026-08-07 -- see docs/domain/FIRMWARE-UPDATE.md.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import ClientError

from custom_components.fermob.firmware import (
    DFU_HOSTS,
    async_get_latest_release,
    slugify_name,
)

H134_RELEASE = {
    "code": 200,
    "data": {
        "message": "",
        "release": {
            "project": "NRF52_Fermob_MOOONH134",
            "version": "3.0.27.0",
            "model": "MOOONH134",
            "file_id": "1de676224749bf085088e167aa3fd45cebb0a109",
            "date": "2023-11-07T09:31:30Z",
            "manufacturer": "Fermob",
        },
    },
}

UNKNOWN_MODEL = {
    "code": 400,
    "data": {"release": {}, "message": "This model doesn't exists"},
}


class _Response:
    def __init__(self, body: Any) -> None:
        self._body = body

    async def json(self, content_type: Any = None) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Session:
    """Enough aiohttp to answer one GET per configured host, in order."""

    def __init__(self, *bodies: Any) -> None:
        self._bodies = list(bodies)
        self.urls: list[str] = []

    def get(self, url: str) -> _Response:
        self.urls.append(url)
        body = self._bodies.pop(0) if self._bodies else None
        if isinstance(body, ClientError):
            raise body
        return _Response(body)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MOOON - H134", "MOOONH134"),
        ("Fermob", "Fermob"),
        ("HOOPIK - GL1200", "HOOPIKGL1200"),
        (" MOOON  -  H63 ", "MOOONH63"),
    ],
)
def test_slugify_name_strips_dashes_and_whitespace(raw, expected):
    """The server 400s on anything else, so this is not cosmetic."""
    assert slugify_name(raw) == expected


async def test_returns_the_published_release():
    session = _Session(H134_RELEASE)

    release = await async_get_latest_release(session, "Fermob", "MOOON - H134")

    assert release is not None
    assert release.version == "3.0.27.0"
    assert release.file_id == "1de676224749bf085088e167aa3fd45cebb0a109"
    assert session.urls == [
        f"https://{DFU_HOSTS[0]}/api/dfu/v1/release/Fermob/MOOONH134/latest"
    ]


async def test_an_unknown_model_is_an_answer_not_a_failure():
    """`code: 400` inside a 200 is the server telling us about a real lamp.

    Retrying that against the fallback host buys nothing -- it holds the same
    catalogue -- so the second request must not happen.
    """
    session = _Session(UNKNOWN_MODEL, H134_RELEASE)

    assert await async_get_latest_release(session, "Fermob", "HOOPIK") is None
    assert len(session.urls) == 1


async def test_falls_back_to_the_second_host_on_a_transport_failure():
    session = _Session(ClientError("boom"), H134_RELEASE)

    release = await async_get_latest_release(session, "Fermob", "MOOON - H134")

    assert release is not None and release.version == "3.0.27.0"
    assert [url.split("/")[2] for url in session.urls] == list(DFU_HOSTS)


async def test_returns_none_when_neither_host_answers():
    session = _Session(ClientError("one"), ClientError("two"))

    assert await async_get_latest_release(session, "Fermob", "MOOON - H134") is None
    assert len(session.urls) == 2


async def test_a_body_that_is_not_json_is_a_transport_failure():
    """A captive portal or proxy error page will 200 with HTML."""
    session = _Session(ValueError("not json"), H134_RELEASE)

    release = await async_get_latest_release(session, "Fermob", "MOOON - H134")

    assert release is not None
    assert len(session.urls) == 2


@pytest.mark.parametrize(
    "body",
    [
        None,
        "a string",
        {"code": 200},
        {"code": 200, "data": {}},
        {"code": 200, "data": {"release": {}}},
        {"code": 200, "data": {"release": {"version": ""}}},
        {"code": 200, "data": {"release": {"version": 3}}},
    ],
)
async def test_a_release_without_a_usable_version_is_none(body):
    """Anything we cannot read a version out of must not become one."""
    assert await async_get_latest_release(_Session(body), "Fermob", "X") is None
