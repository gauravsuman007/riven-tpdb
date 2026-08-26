"""What a VPN provider has to offer, and nothing about how it does it.

The point of this seam is that Tailscale is one implementation of it. Every
caller in the codebase asks the *service* for a proxy and never names a
provider, so swapping in WireGuard or a commercial VPN later means writing one
class and adding one enum value, not revisiting the scrapers.

The contract is deliberately narrow: a provider connects, reports what it is
doing, optionally offers a list of exit nodes, and hands back a proxy URL.
Routing *policy* -- which traffic is allowed through it -- is not a provider
concern. That lives in the service, because it is the same question whatever
is on the other end of the tunnel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ExitNode:
    """A node traffic can be routed through."""

    id: str
    name: str
    country: str | None = None
    online: bool = True
    active: bool = False


@dataclass(slots=True)
class VpnStatus:
    """What the provider is currently doing.

    ``connected`` means traffic can actually be routed right now -- not that
    the daemon is reachable, and not that credentials exist. Callers gate on
    this, so anything weaker would send traffic into a tunnel that is not up.
    """

    provider: str
    connected: bool = False
    # Free text for the UI. A daemon that is unreachable, a key that expired
    # and a login that is half-finished are very different problems and the
    # user can only act on them if they are told which one it is.
    state: str = "disabled"
    detail: str | None = None
    # Present while an interactive login is waiting for the user to visit it.
    auth_url: str | None = None
    hostname: str | None = None
    exit_node: str | None = None
    exit_nodes: list[ExitNode] = field(default_factory=list)


@runtime_checkable
class VpnProvider(Protocol):
    """One way of getting traffic out through somewhere else."""

    key: str

    def status(self) -> VpnStatus:
        """Current state. Must never raise -- report the problem instead.

        Called on every settings page load and before routed requests, so a
        provider that threw would take those paths down with it rather than
        simply being unavailable.
        """
        ...

    def proxy_url(self) -> str | None:
        """A proxy URL routed traffic should use, or None when unavailable.

        Returning None is the safe answer: callers fall back to a direct
        connection only when policy allows it, and never silently tunnel.
        """
        ...

    def connect(self, auth_key: str | None = None) -> VpnStatus:
        """Bring the tunnel up.

        With ``auth_key``, authenticate non-interactively. Without one, begin
        an interactive login and return a status carrying ``auth_url`` for the
        user to visit.
        """
        ...

    def disconnect(self) -> None:
        """Take the tunnel down, leaving credentials alone."""
        ...

    def set_exit_node(self, node_id: str | None) -> VpnStatus:
        """Route through ``node_id``, or stop using an exit node when None."""
        ...
