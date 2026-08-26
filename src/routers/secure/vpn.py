"""VPN status and control.

Thin on purpose. Every decision -- which provider, whether a purpose is
routed, what happens when the tunnel is down -- lives in
:mod:`program.services.vpn`, because those are the same questions whatever is
on the other end of the tunnel. This layer only exposes them.
"""

from typing import Annotated

from fastapi import APIRouter, Body
from loguru import logger
from pydantic import BaseModel

from program.services.vpn import reset, vpn
from program.services.vpn.base import VpnStatus
from program.settings import settings_manager

router = APIRouter(prefix="/vpn", tags=["vpn"])


class ExitNodeModel(BaseModel):
    id: str
    name: str
    country: str | None = None
    online: bool = True
    active: bool = False


class VpnStatusResponse(BaseModel):
    provider: str
    enabled: bool
    connected: bool
    state: str
    detail: str | None = None
    # Present while an interactive login is waiting to be visited.
    auth_url: str | None = None
    hostname: str | None = None
    exit_node: str | None = None
    # Resolved server-side so callers never have to cross-reference
    # exit_nodes themselves -- every caller of this endpoint (the direct-search
    # panel, the streaming banner, the settings tab) would otherwise duplicate
    # the same lookup.
    exit_node_name: str | None = None
    exit_nodes: list[ExitNodeModel] = []
    route_scraping: bool = False
    route_streaming: bool = False


def _response(status: VpnStatus) -> VpnStatusResponse:
    settings = settings_manager.settings.vpn

    return VpnStatusResponse(
        provider=status.provider,
        enabled=settings.enabled,
        connected=status.connected,
        state=status.state,
        detail=status.detail,
        auth_url=status.auth_url,
        hostname=status.hostname,
        exit_node=status.exit_node,
        exit_node_name=next(
            (node.name for node in status.exit_nodes if node.active), None
        ),
        exit_nodes=[
            ExitNodeModel(
                id=node.id,
                name=node.name,
                country=node.country,
                online=node.online,
                active=node.active,
            )
            for node in status.exit_nodes
        ],
        route_scraping=settings.route_scraping,
        route_streaming=settings.route_streaming,
    )


@router.get("/status", operation_id="vpn_status")
def vpn_status() -> VpnStatusResponse:
    """Current tunnel state, including which exit nodes are on offer."""

    return _response(vpn().status(refresh=True))


class ConnectBody(BaseModel):
    # Optional: with a key this authenticates non-interactively, without one it
    # starts an interactive login and the URL to visit comes back in the status.
    auth_key: str | None = None


@router.post("/connect", operation_id="vpn_connect")
def vpn_connect(
    body: Annotated[ConnectBody, Body()] = ConnectBody(),
) -> VpnStatusResponse:
    """Bring the tunnel up, by auth key or interactive login.

    A key supplied here is saved, because the alternative is asking for it
    again after every restart. A key already in settings is used when none is
    supplied, which is what makes the tunnel come back on its own.
    """

    settings = settings_manager.settings.vpn
    key = (body.auth_key or "").strip() or settings.tailscale.auth_key or None

    if body.auth_key and body.auth_key.strip():
        settings.tailscale.auth_key = body.auth_key.strip()
        settings_manager.save()

    # The service caches its settings at construction, so a key or a toggle
    # saved a moment ago would otherwise not be seen until a restart.
    reset()

    logger.debug(f"VPN connect requested ({'key' if key else 'interactive'})")

    return _response(vpn().connect(key))


@router.post("/disconnect", operation_id="vpn_disconnect")
def vpn_disconnect() -> VpnStatusResponse:
    """Stop routing. Deliberately not a logout -- the machine stays authorised."""

    return _response(vpn().disconnect())


class ExitNodeBody(BaseModel):
    # None clears the exit node and routes out of the tailnet directly.
    node_id: str | None = None


@router.post("/exit-node", operation_id="vpn_set_exit_node")
def vpn_set_exit_node(
    body: Annotated[ExitNodeBody, Body()] = ExitNodeBody(),
) -> VpnStatusResponse:
    """Choose which node routed traffic leaves from."""

    return _response(vpn().set_exit_node(body.node_id))
