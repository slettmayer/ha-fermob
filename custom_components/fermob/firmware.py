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

from aiohttp import ClientError, ClientSession

_LOGGER = logging.getLogger(__name__)

# Both hosts serve the same content; the app treats the second as a fallback and
# so do we. Hardcoded in the app as `dfuServerUrl` / `dfuServerUrlFallback`.
DFU_HOSTS = ("dfu1.smartandgreen.eu", "dfu2.smartandgreen.eu")
DFU_API_VERSION = "1"

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
    return (
        f"https://{host}/api/dfu/v{DFU_API_VERSION}"
        f"/release/{slugify_name(manufacturer)}/{slugify_name(model)}/latest"
    )


def _parse_release(body: Any) -> FirmwareRelease | None:
    """Read a release out of the server's envelope, or None if it has none.

    The server answers `200` with an envelope carrying its own `code`, and an
    unknown model is `code: 400` with `"This model doesn't exists"` -- a real
    answer about a real lamp, not a transport failure, so it must not be retried
    against the fallback host.
    """
    if not isinstance(body, dict) or body.get("code") != 200:
        return None
    release = (body.get("data") or {}).get("release") or {}
    version = release.get("version")
    if not isinstance(version, str) or not version:
        return None
    return FirmwareRelease(
        version=version,
        file_id=release.get("file_id") or "",
        date=release.get("date"),
    )


async def async_get_latest_release(
    session: ClientSession, manufacturer: str, model: str
) -> FirmwareRelease | None:
    """Ask for the newest published release for one lamp model.

    Returns None both for "this model is not on the server" and for "the server
    could not be reached" -- the caller keeps whatever it knew before either
    way, and neither is worth surfacing to a user who cannot act on it. The
    Hoopik is one of the models the server does not know; see
    [docs/domain/FIRMWARE-UPDATE.md](../../docs/domain/FIRMWARE-UPDATE.md).
    """
    for host in DFU_HOSTS:
        url = _release_url(host, manufacturer, model)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with session.get(url) as response:
                    body = await response.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as err:
            # ValueError covers a body that is not JSON at all, which a captive
            # portal or a proxy error page will happily return with a 200.
            _LOGGER.debug("Fermob: firmware check against %s failed: %s", host, err)
            continue

        release = _parse_release(body)
        if release is not None:
            return release
        # A reachable host that has no release for this model will say the same
        # thing twice, so stop rather than paying for the fallback.
        _LOGGER.debug("Fermob: %s has no release for %s/%s", host, manufacturer, model)
        return None
    return None
