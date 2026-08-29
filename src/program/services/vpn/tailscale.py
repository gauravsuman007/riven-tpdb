"""Tailscale, driven over its local API.

The daemon runs as a sidecar container in userspace networking mode, which is
the whole reason this integration can be selective. Kernel mode captures the
container's entire routing table -- every TPDB call, every debrid request, the
library scan -- and there would be no way to send only the scrapers through it.
Userspace mode instead exposes a SOCKS5 proxy and routes exactly what is
pointed at it, which is what "scraper traffic only" actually requires.

Control happens through ``tailscaled``'s local API over its unix socket, shared
into this container by the compose file. That is the same interface the
``tailscale`` CLI uses. It is not a versioned public API, so every call here is
defensive: a shape that is not recognised degrades to "unavailable" and the
tunnel simply is not used, rather than taking the scrapers down.
"""

from __future__ import annotations

import json

import httpx
from loguru import logger

from program.services.vpn.base import ExitNode, VpnProvider, VpnStatus

# The local API insists on this Host header regardless of transport.
_LOCAL_HOST = "http://local-tailscaled.sock"

# Backend states tailscaled reports, mapped to whether traffic can flow.
_RUNNING = "Running"


class TailscaleProvider(VpnProvider):
    key = "tailscale"

    def __init__(self, socket_path: str, proxy: str, timeout: float = 10.0):
        self.socket_path = socket_path
        self._proxy = proxy
        self.timeout = timeout

    # ------------------------------------------------------------- transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            transport=httpx.HTTPTransport(uds=self.socket_path),
            base_url=_LOCAL_HOST,
            timeout=self.timeout,
        )

    def _call(self, method: str, path: str, **kwargs) -> dict | None:
        """One local-API call. Returns None on any failure, having logged it."""

        try:
            with self._client() as client:
                response = client.request(method, path, **kwargs)

                if response.status_code >= 400:
                    # WARNING, not debug: a write here failing is invisible
                    # anywhere else. `set_exit_node` (below) does not treat
                    # this as a hard error -- it re-reads status regardless,
                    # by design, since a stale read on a flaky connection is
                    # not worth turning into a user-facing failure -- so this
                    # line is the only place a rejected write is ever
                    # recorded. Confirmed live: tailscaled answers prefs
                    # WRITES with 403 for any caller that is not root or its
                    # configured operator, with no other symptom -- the
                    # request "succeeds" from Riven's side, the exit node
                    # picker just silently reverts on reload.
                    logger.warning(
                        f"tailscale {method} {path} -> {response.status_code}: "
                        f"{response.text[:200]}"
                    )
                    return None

                if not response.content:
                    return {}

                return response.json()
        except FileNotFoundError:
            # The socket is not there at all: the sidecar is not running, or
            # the volume is not shared. Common enough to be worth naming.
            logger.debug(f"tailscale socket missing at {self.socket_path}")
            return None
        except (httpx.HTTPError, json.JSONDecodeError, OSError) as exc:
            logger.debug(f"tailscale {method} {path} failed: {exc}")
            return None

    # ---------------------------------------------------------------- status

    def status(self) -> VpnStatus:
        raw = self._call("GET", "/localapi/v0/status")

        if raw is None:
            return VpnStatus(
                provider=self.key,
                state="unreachable",
                detail=(
                    "The Tailscale sidecar is not reachable. Check that the "
                    "tailscale service is running and shares its state volume."
                ),
            )

        backend = raw.get("BackendState") or "unknown"
        self_node = raw.get("Self") or {}

        status = VpnStatus(
            provider=self.key,
            connected=backend == _RUNNING,
            state=backend.lower(),
            auth_url=raw.get("AuthURL") or None,
            hostname=self_node.get("HostName"),
        )

        if backend == "NeedsLogin":
            status.detail = "Not logged in yet."
        elif backend == "Stopped":
            status.detail = "Logged in, but the tunnel is stopped."

        status.exit_nodes = self._exit_nodes(raw)

        for node in status.exit_nodes:
            if node.active:
                status.exit_node = node.id

        return status

    @staticmethod
    def _exit_nodes(raw: dict) -> list[ExitNode]:
        """Peers advertising themselves as exit nodes.

        Only peers that actually offer to be one are listed. Every other peer
        on the tailnet is a machine the user owns, not a way out, and offering
        them as choices would invite selecting one that silently routes
        nothing.
        """

        nodes: list[ExitNode] = []

        for peer in (raw.get("Peer") or {}).values():
            if not peer.get("ExitNodeOption"):
                continue

            name = peer.get("HostName") or peer.get("DNSName") or "unknown"
            nodes.append(
                ExitNode(
                    id=peer.get("ID") or name,
                    name=name.rstrip("."),
                    country=(peer.get("Location") or {}).get("Country"),
                    online=bool(peer.get("Online")),
                    active=bool(peer.get("ExitNode")),
                )
            )

        nodes.sort(key=lambda n: (not n.online, n.name.lower()))
        return nodes

    # ------------------------------------------------------------ connection

    def proxy_url(self) -> str | None:
        """The SOCKS5 proxy, but only while the tunnel is actually up.

        Gated on status rather than returned unconditionally: the proxy address
        exists whether or not Tailscale is logged in, and handing it out while
        the tunnel is down produces connection errors that look like the site
        is blocking us.
        """

        return self._proxy if self.status().connected else None

    def connect(self, auth_key: str | None = None) -> VpnStatus:
        # WantRunning first in both cases. Authenticating alone leaves the
        # daemon logged in but not routing, which reports as "Stopped" and
        # looks like the login silently failed.
        self._call(
            "PATCH",
            "/localapi/v0/prefs",
            json={"WantRunning": True, "WantRunningSet": True},
        )

        if auth_key:
            self._call("POST", "/localapi/v0/login", params={"authkey": auth_key})
        else:
            # Returns immediately; the URL to visit turns up in status.
            self._call("POST", "/localapi/v0/login-interactive")

        return self.status()

    def disconnect(self) -> None:
        """Stop routing, but stay logged in.

        Deliberately not a logout: the user switching the tunnel off in
        settings expects to be able to switch it back on, not to re-
        authenticate the machine.
        """

        self._call(
            "PATCH",
            "/localapi/v0/prefs",
            json={"WantRunning": False, "WantRunningSet": True},
        )

    def set_exit_node(self, node_id: str | None) -> VpnStatus:
        current = self.status()

        if node_id is None:
            self._call(
                "PATCH",
                "/localapi/v0/prefs",
                json={"ExitNodeID": "", "ExitNodeIDSet": True},
            )
            after = self.status()

            # `_call` already logged a 403/etc at warning level, but that is
            # invisible to whoever is looking at the settings page rather
            # than the container logs -- and a rejected write and a genuinely
            # applied one look identical from here otherwise: both return 200
            # from THIS endpoint, because `status()` always succeeds even
            # when the PATCH just before it did not. Confirmed live: the
            # daemon rejects prefs writes from any caller that is not root or
            # its configured operator, and every symptom of that is silent
            # right up to this check.
            if after.exit_node is not None:
                after.detail = (
                    "Tailscale rejected clearing the exit node. This is "
                    "usually a permissions problem between containers, not "
                    "a setting to retry -- check the tailscale sidecar's "
                    "logs."
                )
            return after

        known = {node.id for node in current.exit_nodes}

        if node_id not in known:
            # Refused rather than passed through: an unknown id is accepted by
            # the daemon and silently routes nothing, which looks identical to
            # a working tunnel from the outside.
            current.detail = f"{node_id!r} is not an available exit node."
            return current

        self._call(
            "PATCH",
            "/localapi/v0/prefs",
            json={"ExitNodeID": node_id, "ExitNodeIDSet": True},
        )
        after = self.status()

        if after.exit_node != node_id:
            after.detail = (
                "Tailscale rejected the exit node change. This is usually a "
                "permissions problem between containers, not a setting to "
                "retry -- check the tailscale sidecar's logs."
            )
        return after
