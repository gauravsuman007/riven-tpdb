"""Routing policy: which traffic goes through the tunnel, and what if it is down.

Callers ask this service, never a provider, and never for "the VPN" in general
-- they ask whether a *particular purpose* is routed. Two purposes exist today,
scraping and streaming, and they are separate because they genuinely differ:
searching a handful of sites is small and occasional, while streaming pushes
gigabytes through whatever exit node was picked. Wanting one routed and not the
other is a reasonable position, not an edge case.

FAILING CLOSED IS THE WHOLE POINT. If a purpose is configured to go through the
tunnel and the tunnel is not up, this raises rather than quietly using a direct
connection. Someone who routes their scraper traffic is doing it to control
where that traffic appears to come from; silently sending it out of the host's
own address the moment the tunnel drops would defeat the only reason the
setting exists, and would do it invisibly.
"""

from __future__ import annotations

import time

from loguru import logger

from program.services.vpn.base import ExitNode, VpnProvider, VpnStatus
from program.services.vpn.gluetun import GluetunProvider
from program.services.vpn.tailscale import TailscaleProvider
from program.settings import settings_manager

SCRAPING = "scraping"
STREAMING = "streaming"


class VpnUnavailable(Exception):
    """A routed purpose was requested while the tunnel was down."""


class VpnService:
    """The only thing the rest of the codebase touches."""

    key = "vpn"

    #: Status is read before routed requests and on every settings load, and
    #: each read is a round trip to the daemon. A couple of seconds is short
    #: enough that switching an exit node feels immediate and long enough that
    #: a page of scraper calls does not ask repeatedly.
    STATUS_TTL = 2.0

    def __init__(self) -> None:
        self.settings = settings_manager.settings.vpn
        self._status: VpnStatus | None = None
        self._checked_at = 0.0

        # Deliberately NOT gated on `self.settings.enabled`. Logging in,
        # checking status and choosing an exit node are account-management
        # actions -- someone setting this up for the first time needs all
        # three before there is any reason to flip "enabled" on, and gating
        # provider construction on it meant "Generate login link" returned a
        # silent, error-free "disabled" status with nothing visibly
        # different from a successful click. `enabled` (together with
        # `route_scraping`/`route_streaming`) still gates ROUTING -- see
        # `routes()` below -- which is the thing it should gate.
        self.provider = self._build()
        self.initialized = self.provider is not None

        if self.initialized:
            logger.success(f"VPN ({self.settings.provider}) initialized!")

    def _build(self) -> VpnProvider | None:
        if self.settings.provider == "tailscale":
            return TailscaleProvider(
                socket_path=self.settings.tailscale.socket_path,
                proxy=self.settings.tailscale.proxy_url,
            )

        if self.settings.provider == "gluetun":
            return GluetunProvider(
                control_url=self.settings.gluetun.control_url,
                proxy=self.settings.gluetun.proxy_url,
                api_key=self.settings.gluetun.api_key,
            )

        logger.warning(f"Unknown VPN provider {self.settings.provider!r}")
        return None

    # ---------------------------------------------------------------- status

    def status(self, refresh: bool = False) -> VpnStatus:
        if self.provider is None:
            return VpnStatus(provider=self.settings.provider, state="disabled")

        now = time.monotonic()

        if refresh or self._status is None or now - self._checked_at > self.STATUS_TTL:
            self._status = self.provider.status()
            self._checked_at = now

        return self._status

    # --------------------------------------------------------------- routing

    def routes(self, purpose: str) -> bool:
        """Whether this purpose is configured to go through the tunnel."""

        if not self.settings.enabled:
            return False

        if purpose == SCRAPING:
            return self.settings.route_scraping

        if purpose == STREAMING:
            return self.settings.route_streaming

        return False

    def proxy_for(self, purpose: str) -> str | None:
        """The proxy URL for this purpose, or None when it is not routed.

        Raises :class:`VpnUnavailable` when the purpose *is* routed but the
        tunnel is down. That is deliberate -- see the module docstring. The
        caller's job is to surface it, not to fall back.
        """

        if not self.routes(purpose):
            return None

        if self.provider is None:
            raise VpnUnavailable(
                f"{purpose} is set to route through the VPN, but no provider "
                f"is configured."
            )

        proxy = self.provider.proxy_url()

        if not proxy:
            status = self.status()
            raise VpnUnavailable(
                f"{purpose} is set to route through the VPN, but it is not "
                f"connected ({status.state}). "
                f"{status.detail or 'Check the VPN tab in settings.'}"
            )

        return proxy

    def proxies_for(self, purpose: str) -> dict[str, str] | None:
        """``requests``-shaped proxies for this purpose, or None."""

        proxy = self.proxy_for(purpose)
        return {"http": proxy, "https": proxy} if proxy else None

    # ------------------------------------------------------------- lifecycle

    def connect(self, auth_key: str | None = None) -> VpnStatus:
        if self.provider is None:
            return VpnStatus(provider=self.settings.provider, state="disabled")

        status = self.provider.connect(auth_key)
        self._status, self._checked_at = status, time.monotonic()
        return status

    def disconnect(self) -> VpnStatus:
        if self.provider is None:
            return VpnStatus(provider=self.settings.provider, state="disabled")

        self.provider.disconnect()
        return self.status(refresh=True)

    def set_exit_node(self, node_id: str | None) -> VpnStatus:
        if self.provider is None:
            return VpnStatus(provider=self.settings.provider, state="disabled")

        status = self.provider.set_exit_node(node_id)
        self._status, self._checked_at = status, time.monotonic()
        return status


_service: VpnService | None = None


def vpn() -> VpnService:
    """The process-wide VPN service.

    A singleton because the status cache is only useful if it is shared, and
    because a provider may hold a connection to the daemon.
    """

    global _service

    if _service is None:
        _service = VpnService()

    return _service


def reset() -> None:
    """Drop the cached service so changed settings take effect."""

    global _service
    _service = None


__all__ = [
    "SCRAPING",
    "STREAMING",
    "ExitNode",
    "VpnService",
    "VpnStatus",
    "VpnUnavailable",
    "reset",
    "vpn",
]
