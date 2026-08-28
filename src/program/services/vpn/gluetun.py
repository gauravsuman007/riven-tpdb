"""Gluetun, driven over its HTTP control server.

Gluetun is normally used by putting other containers on its network stack
(``network_mode: service:gluetun``), which routes *everything* they do through
the tunnel. That is the one thing this integration must not do: TPDB, the
debrid providers, the indexers and the library scan are all supposed to go out
directly, and only the scrapers and streaming are routed. So Gluetun is used
here the same way Tailscale is -- as a proxy that traffic is pointed at
explicitly -- via its built-in HTTP proxy rather than by joining its network.

That means the compose service needs ``HTTPPROXY=on`` and its port published to
this container, and it must NOT be this container's network_mode. Without the
proxy switched on there is nothing to point at, and `status()` says so rather
than reporting a tunnel that cannot actually carry anything.

Control is the HTTP control server (``HTTP_CONTROL_SERVER_ADDRESS``, :8000 by
default). Its routes moved between major versions -- ``/v1/openvpn/status``
became ``/v1/vpn/status`` when WireGuard was added -- so both are tried, and an
unrecognised shape degrades to "unavailable" rather than taking the scrapers
down with it. From v3.40 the control server can require an API key, which is
sent when one is configured.
"""

from __future__ import annotations

import json

import httpx
from loguru import logger

from program.services.vpn.base import ExitNode, VpnProvider, VpnStatus

#: Newer builds first: a v3.40+ Gluetun answers both, but only the generic one
#: is correct when the tunnel is WireGuard.
_STATUS_PATHS = ("/v1/vpn/status", "/v1/openvpn/status")


class GluetunProvider(VpnProvider):
    key = "gluetun"

    def __init__(
        self,
        control_url: str,
        proxy: str,
        api_key: str = "",
        timeout: float = 10.0,
    ):
        self.control_url = control_url.rstrip("/")
        self._proxy = proxy
        self.api_key = api_key
        self.timeout = timeout

        #: Learned on the first successful status call and reused, so a
        #: version that only answers the legacy path is not re-probed on
        #: every request.
        self._status_path: str | None = None

    # ------------------------------------------------------------- transport

    def _call(self, method: str, path: str, **kwargs) -> dict | None:
        """One control-server call. Returns None on any failure, having logged it."""

        headers = {"X-API-Key": self.api_key} if self.api_key else {}

        try:
            with httpx.Client(base_url=self.control_url, timeout=self.timeout) as client:
                response = client.request(method, path, headers=headers, **kwargs)

                if response.status_code == 401:
                    logger.debug(
                        f"gluetun {method} {path} -> 401; the control server "
                        f"wants an API key (see the VPN tab)"
                    )
                    return None

                if response.status_code >= 400:
                    logger.debug(
                        f"gluetun {method} {path} -> {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                    return None

                if not response.content:
                    return {}

                return response.json()
        except (httpx.HTTPError, json.JSONDecodeError, OSError) as exc:
            logger.debug(f"gluetun {method} {path} failed: {exc}")
            return None

    def _status_call(self) -> tuple[str, dict] | None:
        """Status from whichever route this Gluetun version answers."""

        paths = (self._status_path,) if self._status_path else _STATUS_PATHS

        for path in paths:
            raw = self._call("GET", path)

            if raw is not None and "status" in raw:
                self._status_path = path
                return path, raw

        # A learned path that stopped working (the container was upgraded
        # under us): forget it and probe again next time.
        self._status_path = None
        return None

    # ---------------------------------------------------------------- status

    def status(self) -> VpnStatus:
        found = self._status_call()

        if found is None:
            return VpnStatus(
                provider=self.key,
                state="unreachable",
                detail=(
                    f"The Gluetun control server at {self.control_url} is not "
                    f"reachable. Check that the container is running, that "
                    f"HTTP_CONTROL_SERVER_ADDRESS is published to this "
                    f"container, and that an API key is set here if Gluetun "
                    f"requires one."
                ),
            )

        _, raw = found

        # "running" | "stopped" | "starting" | "stopping"
        state = str(raw.get("status") or "unknown").lower()
        running = state == "running"

        status = VpnStatus(
            provider=self.key,
            connected=running,
            state=state,
        )

        if running:
            # The public IP is the only honest confirmation that traffic is
            # actually leaving where it should. Purely informational -- a
            # failure here does not make the tunnel unusable.
            ip = self._call("GET", "/v1/publicip/ip") or {}
            location = ", ".join(
                str(part)
                for part in (ip.get("city"), ip.get("country"))
                if part
            )

            status.hostname = ip.get("public_ip") or None
            status.detail = f"Connected via {location}." if location else None

            # Not a peer list like Tailscale's: with Gluetun the exit point is
            # the provider's server, chosen by the container's own env
            # (VPN_SERVER_COUNTRIES and friends) at startup. It is reported as
            # a single, non-selectable entry so the UI can show where traffic
            # is going without implying it can be changed from here.
            if location:
                status.exit_node = location
                status.exit_nodes = [
                    ExitNode(
                        id=location,
                        name=location,
                        country=ip.get("country"),
                        online=True,
                        active=True,
                    )
                ]
        elif state == "stopped":
            status.detail = "The tunnel is stopped."

        return status

    # --------------------------------------------------------------- routing

    def proxy_url(self) -> str | None:
        """The configured HTTP proxy, but only while the tunnel is actually up.

        Gluetun's proxy keeps accepting connections when the tunnel is down --
        it is a separate listener -- so returning it unconditionally would
        hand callers a proxy that quietly forwards traffic outside the tunnel.
        That is the exact failure this whole service exists to prevent, so the
        status is checked first.
        """

        if not self._proxy:
            return None

        return self._proxy if self.status().connected else None

    # ------------------------------------------------------------- lifecycle

    def connect(self, auth_key: str | None = None) -> VpnStatus:
        """Start the tunnel.

        ``auth_key`` is ignored: Gluetun takes its provider credentials from
        the container's own environment at startup, and there is no
        interactive login to begin. The parameter exists because the provider
        interface is shared with Tailscale, which does have one.
        """

        self._call("PUT", self._status_path or _STATUS_PATHS[0], json={"status": "running"})
        return self.status()

    def disconnect(self) -> None:
        self._call("PUT", self._status_path or _STATUS_PATHS[0], json={"status": "stopped"})

    def set_exit_node(self, node_id: str | None) -> VpnStatus:
        """Not selectable at runtime, and said so rather than silently ignored.

        Which server Gluetun uses is fixed by its environment
        (VPN_SERVER_COUNTRIES / VPN_SERVER_CITIES / SERVER_HOSTNAMES) when the
        container starts. Accepting a node id here and doing nothing would
        leave the UI showing a choice that never took effect.
        """

        status = self.status()
        status.detail = (
            "Gluetun's exit server is set by the container's own environment "
            "(VPN_SERVER_COUNTRIES and similar) and cannot be changed from "
            "here. Edit docker-compose.yml and restart the gluetun service."
        )
        return status
