"""Client for the vendor firmware-release server.

The one thing in this integration that is not local. Linkio -- the protocol
vendor, not Fermob -- publishes the latest firmware release per lamp model at
`dfu{1,2}.smartandgreen.eu`, and the official app asks it the same question
before offering an update. We ask only *whether* a newer build exists; we never
download it, because this integration cannot install one. See
[docs/domain/FIRMWARE-UPDATE.md](../../docs/domain/FIRMWARE-UPDATE.md).

Deliberately free of `homeassistant` imports, like `protocol.py`: the caller
passes the session in, so every branch here is testable without a `hass`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, NamedTuple
from urllib.parse import quote

from aiohttp import ClientError, ClientSession

_LOGGER = logging.getLogger(__name__)

# Both hosts serve the same content; the app treats the second as a fallback and
# so do we. Hardcoded in the app as `dfuServerUrl` / `dfuServerUrlFallback`.
DFU_HOSTS = ("dfu1.smartandgreen.eu", "dfu2.smartandgreen.eu")
DFU_API_VERSION = "1"

# The envelope code for "This model doesn't exists" -- the one non-200 code that
# is an answer about the lamp rather than a failure of the server.
MODEL_UNKNOWN_CODE = 400

# Short on purpose: nothing waits on this and a diagnostic entity must never be
# the reason a poll interval overruns.
REQUEST_TIMEOUT = 15.0


class FirmwareRelease(NamedTuple):
    """The latest release the server has for one model."""

    version: str
    file_id: str
    date: str | None


def slugify_name(text: str) -> str:
    """Turn a lamp's reported name into the form the server keys on.

    `MOOON - H134` -> `MOOONH134`. The app strips exactly `-` and whitespace
    (`replace(/-|\\s/g,"")`) from both the manufacturer and the model before
    putting them in the path, and the server 400s on anything else.
    """
    return re.sub(r"[-\s]", "", text)


def _release_url(host: str, manufacturer: str, model: str) -> str:
    """Build the release URL, escaping both lamp-supplied path segments.

    `manufacturer` and `model` are strings a *lamp* sent us, and nothing upstream
    constrains them: `protocol._ascii_field` strips NULs and whitespace, and
    `slugify_name` removes dashes. A model containing `/`, `?` or `#` would
    otherwise rewrite the path or bolt a query onto it, and whatever came back
    would be parsed as a release envelope. Percent-encoding with `safe=""` keeps
    every segment a segment.
    """
    return (
        f"https://{host}/api/dfu/v{DFU_API_VERSION}"
        f"/release/{quote(slugify_name(manufacturer), safe='')}"
        f"/{quote(slugify_name(model), safe='')}/latest"
    )


def _parse_release(body: Any) -> tuple[FirmwareRelease | None, bool]:
    """Read the server's envelope: `(release, definitive)`.

    `definitive` means **a working server answered about this model** -- either
    with a release, or with the `code: 400` / `"This model doesn't exists"` that
    is a real answer and not a failure. Anything else (an HTTP-200 body carrying
    `code: 500`, a maintenance page, a shape we do not recognise) is *not*
    definitive: it says nothing about the lamp, so the caller must go on to the
    fallback host rather than treat it as "no update".

    Nothing in here may raise. The envelope is third-party JSON and every level
    of it is checked, because an exception escaping to `async_update` costs a
    traceback per poll and still teaches the entity nothing.
    """
    if not isinstance(body, dict):
        return None, False
    code = body.get("code")
    if code == MODEL_UNKNOWN_CODE:
        return None, True
    if code != 200:
        return None, False
    data = body.get("data")
    release = data.get("release") if isinstance(data, dict) else None
    if not isinstance(release, dict):
        return None, False
    version = release.get("version")
    if not isinstance(version, str) or not version:
        return None, False
    file_id = release.get("file_id")
    date = release.get("date")
    return (
        FirmwareRelease(
            version=version,
            file_id=file_id if isinstance(file_id, str) else "",
            date=date if isinstance(date, str) else None,
        ),
        True,
    )


async def async_get_latest_release(
    session: ClientSession, manufacturer: str, model: str
) -> FirmwareRelease | None:
    """Ask for the newest published release for one lamp model.

    Returns None both for "this model is not on the server" and for "no server
    would answer" -- the caller cannot act differently on the two, and treats
    either as *unknown* rather than as up to date. The Hoopik is one of the
    models the server does not know; see
    [docs/domain/FIRMWARE-UPDATE.md](../../docs/domain/FIRMWARE-UPDATE.md).

    **Only a definitive answer stops us trying the fallback host.** The two hosts
    hold the same catalogue, so re-asking about a model the first one has never
    heard of buys nothing -- but a first host that is *reachable and broken*
    (HTTP 5xx, a JSON error envelope, a maintenance page) has told us nothing
    about the lamp, and skipping the fallback there turns a one-host outage into
    a permanent silent "no update".
    """
    for host in DFU_HOSTS:
        url = _release_url(host, manufacturer, model)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with session.get(url) as response:
                    status = response.status
                    body = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            # ValueError covers a body that is not JSON at all, which a captive
            # portal or a proxy error page will happily return with a 200.
            _LOGGER.debug("Fermob: firmware check against %s failed: %s", host, err)
            continue

        # **The envelope is read before the status is judged, and that order
        # matters.** The server answers an unknown model with HTTP **400** --
        # verified live, 2026-08-07 -- carrying the `code: 400` envelope that is a
        # real answer about a real lamp. Gating on the status first would discard
        # it, ask the fallback host the same question, and log a host failure for
        # what is simply "no such model".
        release, definitive = _parse_release(body)
        if release is not None:
            return release
        if definitive:
            _LOGGER.debug("Fermob: %s has no release for %s", host, model)
            return None
        if status != 200:
            _LOGGER.debug("Fermob: %s answered HTTP %s for %s", host, status, model)
        else:
            _LOGGER.debug(
                "Fermob: %s returned an unusable envelope for %s", host, model
            )
    return None
