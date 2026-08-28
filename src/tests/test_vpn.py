"""VPN routing policy, and the failure mode that matters.

The interesting behaviour here is not "does it proxy". It is what happens when
a purpose is configured to go through the tunnel and the tunnel is not up. The
answer has to be "refuse", because the alternative -- quietly using the host's
own connection -- defeats the only reason anyone routes this traffic, and does
it without a symptom anyone would notice.

Stdlib only; the provider is a stub, since what is under test is the policy.
"""

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))


class _Logger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


sys.modules.setdefault("loguru", types.ModuleType("loguru"))
sys.modules["loguru"].logger = _Logger()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


VPN = SRC / "program" / "services" / "vpn"

for pkg in ("program", "program.services", "program.services.vpn"):
    sys.modules.setdefault(pkg, types.ModuleType(pkg))

base = _load("program.services.vpn.base", VPN / "base.py")


class _Settings:
    class vpn:
        enabled = True
        provider = "tailscale"
        route_scraping = True
        route_streaming = False

        class tailscale:
            socket_path = "/nonexistent.sock"
            proxy_url = "socks5h://tailscale:1055"


_sm = types.ModuleType("program.settings")
_sm.settings_manager = types.SimpleNamespace(settings=_Settings)
sys.modules["program.settings"] = _sm

# The real provider talks to a daemon over a unix socket; the policy under
# test never needs one.
_ts = types.ModuleType("program.services.vpn.tailscale")


class _StubProvider:
    key = "tailscale"

    def __init__(self, connected=True):
        self.connected = connected
        self.disconnected = False

    def status(self):
        return base.VpnStatus(
            provider="tailscale",
            connected=self.connected,
            state="running" if self.connected else "needslogin",
        )

    def proxy_url(self):
        return "socks5h://tailscale:1055" if self.connected else None

    def connect(self, auth_key=None):
        self.connected = True
        return self.status()

    def disconnect(self):
        self.connected = False
        self.disconnected = True

    def set_exit_node(self, node_id):
        return self.status()


_ts.TailscaleProvider = lambda **kwargs: _StubProvider()
sys.modules["program.services.vpn.tailscale"] = _ts

vpn_mod = _load("program.services.vpn.service_under_test", VPN / "__init__.py")

PASSED, FAILED = [], []


def check(name, fn):
    try:
        fn()
    except AssertionError as exc:
        FAILED.append((name, str(exc)))
    except Exception as exc:  # noqa: BLE001
        FAILED.append((name, f"{type(exc).__name__}: {exc}"))
    else:
        PASSED.append(name)


def _service(connected=True, **overrides):
    for key, value in overrides.items():
        setattr(_Settings.vpn, key, value)

    service = vpn_mod.VpnService()
    service.provider = _StubProvider(connected=connected)
    service._status = None
    return service


def _reset_settings():
    _Settings.vpn.enabled = True
    _Settings.vpn.route_scraping = True
    _Settings.vpn.route_streaming = False


# ------------------------------------------------------------ fail closed


def test_a_routed_purpose_refuses_when_the_tunnel_is_down():
    """The whole point. Falling back to a direct connection here would send
    the traffic out of the host's own address -- silently, and precisely when
    the user had asked for the opposite."""

    _reset_settings()
    service = _service(connected=False)

    raised = False
    try:
        service.proxy_for(vpn_mod.SCRAPING)
    except vpn_mod.VpnUnavailable:
        raised = True

    assert raised, "returned a direct connection instead of refusing"


def test_the_refusal_says_what_is_wrong():
    """"Search failed" is not actionable; "the VPN is not connected" is."""

    _reset_settings()
    service = _service(connected=False)

    try:
        service.proxy_for(vpn_mod.SCRAPING)
        raise AssertionError("did not raise")
    except vpn_mod.VpnUnavailable as exc:
        assert "not connected" in str(exc).lower()


def test_an_unrouted_purpose_is_not_affected_by_the_tunnel_being_down():
    """Streaming is off here, so playback must work regardless."""

    _reset_settings()
    service = _service(connected=False)

    assert service.proxy_for(vpn_mod.STREAMING) is None


# --------------------------------------------------------------- routing


def test_a_routed_purpose_gets_the_proxy():
    _reset_settings()
    service = _service(connected=True)

    assert service.proxy_for(vpn_mod.SCRAPING) == "socks5h://tailscale:1055"


def test_an_unrouted_purpose_gets_nothing_even_when_connected():
    """Being connected is not permission to route everything through it."""

    _reset_settings()
    service = _service(connected=True)

    assert service.proxy_for(vpn_mod.STREAMING) is None


def test_the_two_purposes_are_independent():
    """The reason there are two switches rather than one."""

    _reset_settings()
    service = _service(connected=True, route_scraping=False, route_streaming=True)

    assert service.proxy_for(vpn_mod.SCRAPING) is None
    assert service.proxy_for(vpn_mod.STREAMING) == "socks5h://tailscale:1055"


def test_disabling_the_vpn_routes_nothing():
    _reset_settings()
    service = _service(connected=True, enabled=False)

    assert service.proxy_for(vpn_mod.SCRAPING) is None
    assert service.proxy_for(vpn_mod.STREAMING) is None
    _Settings.vpn.enabled = True


def test_requests_shaped_proxies_cover_both_schemes():
    """A scraper that proxied http but not https would leak every https call,
    which is all of them."""

    _reset_settings()
    proxies = _service(connected=True).proxies_for(vpn_mod.SCRAPING)

    assert proxies == {
        "http": "socks5h://tailscale:1055",
        "https": "socks5h://tailscale:1055",
    }


# ------------------------------------------------------- leak-proofing


def test_every_scraper_request_goes_through_the_routed_session():
    """Guard the session-level hook.

    Applying the proxy in the scrapers' `_get` helper looks equivalent and is
    not: iporntv calls `self.session.head` directly to probe a rendition, and
    that request would go out around the tunnel while everything else went
    through it. The scraper still works and the video still plays, so nothing
    looks wrong -- only the exit address is.
    """

    text = (SRC / "program/services/directscrapers/base.py").read_text()

    assert "class _RoutedSession(requests.Session)" in text
    assert "def request(self, method, url, **kwargs)" in text
    assert "self.session = _RoutedSession()" in text, (
        "scrapers build a plain requests.Session, so routing is bypassed"
    )


def test_dns_is_resolved_at_the_exit_node():
    """socks5h, not socks5. With plain socks5 the hostname is resolved
    locally, which hands every scraped site to the host's own resolver --
    the exact thing routing the traffic was meant to avoid."""

    text = (SRC / "program/settings/models.py").read_text()
    section = text[text.index("class TailscaleModel"):]
    section = section[: section.index("class VpnModel")]

    assert "socks5h://" in section


def test_the_sidecar_stays_in_userspace_mode():
    """Kernel mode captures the whole container's routing table, which would
    silently route TPDB, the debrid provider and the library scan too."""

    # The image ships only /riven/src, so the compose file is absent when this
    # runs on the server. Skipped rather than failed: a test that fails purely
    # because of where it is run teaches people to ignore red.
    path = SRC.parent / "docker-compose.yml"

    if not path.exists():
        return

    compose = path.read_text()
    section = compose[compose.index("    tailscale:"):]
    section = section[: section.index("    riven_postgres:")]

    assert "TS_USERSPACE=true" in section

    # Checked as an actual capability grant, not as the string appearing
    # somewhere: the service's own comment explains why NET_ADMIN is absent,
    # and a bare substring test fails on the explanation.
    grants = [
        line for line in section.splitlines()
        if "cap_add" in line or "/dev/net/tun" in line
    ]
    grants = [line for line in grants if not line.strip().startswith("#")]

    assert not grants, f"kernel-mode capabilities granted: {grants}"


# --------------------------------------------------------- settings wiring


def test_toggling_vpn_enabled_resets_the_cached_service():
    """Guard the shipped Program.start wiring.

    The VPN service caches its settings at construction, same as brochure and
    awards. Those have a dedicated "set enabled" endpoint that calls
    refresh_content_jobs(); VPN is edited through the generic settings form
    instead, which has no such hook. Without an observer, flipping
    vpn.enabled saves correctly and the already-built service keeps
    answering as if nothing changed until something unrelated rebuilds it --
    exactly what was found live: status reported enabled=true, state=disabled.
    """

    text = (SRC / "program/program.py").read_text()

    assert "settings_manager.register_observer(_reset_vpn_service)" in text, (
        "no observer resets the VPN service on a settings change"
    )

    body = text[text.index("def _reset_vpn_service"):]
    body = body[: body.index("class Program")]

    assert "from program.services.vpn import reset" in body
    assert "reset()" in body


def test_the_socket_is_pinned_into_the_shared_volume():
    """Found live on first deploy: the image's real socket defaults to
    /tmp/tailscaled.sock inside the sidecar, and /var/run/tailscale is only a
    compatibility symlink to it. Sharing that directory alone shares the
    symlink, not the socket -- status read "unreachable" with a healthy,
    logged-in sidecar on the other end. TS_SOCKET is what makes the daemon
    bind its real socket inside the shared directory instead."""

    path = SRC.parent / "docker-compose.yml"

    if not path.exists():
        return

    compose = path.read_text()
    section = compose[compose.index("    tailscale:"):]
    section = section[: section.index("    riven_postgres:")]

    assert "TS_SOCKET=/var/run/tailscale/tailscaled.sock" in section


# ------------------------------------------------------ exit node naming


def test_the_active_exit_nodes_name_is_resolved_server_side():
    """Guard the shipped _response in routers/secure/vpn.py.

    Every caller of GET /vpn/status (the direct-search panel, the streaming
    banner, the settings tab) needs the active exit node's name to build its
    message. Resolving it here once means none of them re-implement the same
    "find the active one in exit_nodes" lookup.
    """

    text = (SRC / "routers/secure/studios.py").parent.joinpath("vpn.py").read_text()

    assert "exit_node_name" in text
    assert "node.active" in text, (
        "the active exit node's name is not resolved in the status response"
    )


# ------------------------------------------------------ deterministic connect


def test_connect_never_substitutes_a_stored_key_for_an_omitted_one():
    """The actual bug behind "the login URL button never shows up".

    The two buttons in the VPN tab are separate precisely so each is
    deterministic: "Generate login link" always means interactive login,
    "Connect with key" always means the typed key. A router that silently
    fell back to whatever key happened to be saved meant the login-link
    button could try key auth instead the moment any key had ever been
    stored -- with no auth_url ever coming back and nothing to click.
    """

    text = (SRC / "routers/secure/vpn.py").read_text()
    body = text[text.index("def vpn_connect("):]
    body = body[: body.index("def vpn_disconnect(")]

    assert "settings.tailscale.auth_key or None" not in body, (
        "connect() falls back to a stored key when the request omitted one"
    )
    assert 'key = (body.auth_key or "").strip() or None' in body


def test_tailscale_settings_are_hidden_from_the_generic_form():
    """Guard the fix for "two auth key fields".

    program/settings/visibility.py is where sections get hidden from the
    schema the settings UI renders from; this checks the VPN entry landed
    there, not just that the mechanism exists.
    """

    text = (SRC / "program/settings/visibility.py").read_text()

    assert '"vpn": frozenset({"tailscale"})' in text


# --------------------------------------------------- construction vs enabled


def test_construction_builds_a_provider_even_when_disabled():
    """The actual bug: "Generate login link" did nothing.

    VpnService used to skip building a provider at all unless
    settings.vpn.enabled was already true, so clicking either login button
    before that toggle was flipped hit `self.provider is None` and came back
    as a silent, error-free "disabled" status -- indistinguishable from a
    successful click. Logging in, checking status and picking an exit node
    are account-management actions that have to work before there is any
    reason to enable routing, not after.
    """

    _reset_settings()
    _Settings.vpn.enabled = False

    service = vpn_mod.VpnService()

    assert service.provider is not None, (
        "no provider was built while vpn.enabled was false, so nothing "
        "the VPN tab's buttons call could ever do anything"
    )
    assert service.initialized is True

    _Settings.vpn.enabled = True


def test_routing_is_still_gated_on_enabled():
    """The fix must not have thrown out the actual gate.

    Building a provider regardless of `enabled` is correct -- login should
    work either way -- but whether traffic actually goes through it must
    still depend on it, or turning "enabled" off would stop being a real
    off switch.
    """

    _reset_settings()
    service = _service(connected=True, enabled=False)

    assert service.proxy_for(vpn_mod.SCRAPING) is None
    assert service.proxy_for(vpn_mod.STREAMING) is None


# ---------------------------------------------------------------- gluetun


def _gluetun(status_payload, ip_payload=None):
    """A GluetunProvider whose control server answers with fixed payloads.

    Loaded directly rather than through the package, matching how the rest of
    this file avoids importing the world to test one rule.
    """

    module = _load("program.services.vpn.gluetun", VPN / "gluetun.py")
    provider = module.GluetunProvider(
        control_url="http://gluetun:8000",
        proxy="http://gluetun:8888",
    )

    def fake_call(method, path, **kwargs):
        if path == "/v1/publicip/ip":
            return ip_payload
        if method == "PUT":
            return {}
        return status_payload

    provider._call = fake_call
    return provider


def test_gluetun_offers_no_proxy_while_the_tunnel_is_down():
    """The failure this whole service exists to prevent.

    Gluetun's HTTP proxy is a separate listener that keeps accepting
    connections when the tunnel is down, so handing it out unconditionally
    would silently forward scraper traffic outside the tunnel -- the exact
    leak, with no symptom.
    """

    assert _gluetun({"status": "stopped"}).proxy_url() is None


def test_gluetun_offers_the_proxy_once_connected():
    provider = _gluetun({"status": "running"}, ip_payload={"public_ip": "1.2.3.4"})

    assert provider.proxy_url() == "http://gluetun:8888"


def test_gluetun_reports_unreachable_rather_than_raising():
    """status() must never raise: it runs before every routed request."""

    module = _load("program.services.vpn.gluetun", VPN / "gluetun.py")
    provider = module.GluetunProvider(control_url="http://nowhere:8000", proxy="")
    provider._call = lambda *a, **k: None

    status = provider.status()

    assert status.connected is False
    assert status.state == "unreachable"
    assert "not reachable" in (status.detail or "")


def test_gluetun_falls_back_to_the_legacy_status_route():
    """The route was renamed when WireGuard was added; both versions exist."""

    module = _load("program.services.vpn.gluetun", VPN / "gluetun.py")
    provider = module.GluetunProvider(control_url="http://gluetun:8000", proxy="http://p:8888")

    seen = []

    def fake_call(method, path, **kwargs):
        seen.append(path)
        if path == "/v1/vpn/status":
            return None  # older build: route does not exist
        if path == "/v1/openvpn/status":
            return {"status": "running"}
        return {}

    provider._call = fake_call

    assert provider.status().connected is True
    assert "/v1/openvpn/status" in seen
    # And it is remembered, rather than re-probed on every call.
    assert provider._status_path == "/v1/openvpn/status"


def test_gluetun_does_not_pretend_the_exit_node_is_selectable():
    """Gluetun's server comes from container env, not from a runtime call.

    Accepting the id and doing nothing would leave the UI showing a choice
    that never took effect.
    """

    provider = _gluetun({"status": "running"}, ip_payload={"country": "Sweden"})
    status = provider.set_exit_node("somewhere-else")

    assert "cannot be changed from here" in (status.detail or "")


for _name, _fn in sorted(list(globals().items())):
    if _name.startswith("test_") and callable(_fn):
        check(_name, _fn)

print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")

for _name, _err in FAILED:
    print(f"  FAIL {_name}: {_err}")

sys.exit(1 if FAILED else 0)
