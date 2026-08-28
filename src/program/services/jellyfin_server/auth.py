"""Authentication for the Jellyfin-compatible surface.

Deliberately NOT a second credential store. Riven has exactly one secret,
`settings.api_key`, and this maps Jellyfin's username/password handshake onto
it: the configured username plus the API key as the password. A client that
authenticates therefore proves it already knew the API key, so the token it
gets back can simply BE that key -- no session table, nothing to expire,
nothing lost across a restart, and no way for this path to grant access the
existing one would not.

The alternative (a real user table with its own passwords) would be a parallel
source of truth for "who may access this server", which is the same trap
`direct_scraping.disabled` and `tailscale.auth_key` both hit from other
directions.

On header formats, the important thing is that we are the SERVER: we do not
choose what the client sends. Jellyfin 10.11 deprecated the `X-Emby-*` forms
in favour of `Authorization: MediaBrowser ...`, but that deprecation is aimed
at client authors, and television apps update slowly or never. All of the
historical forms are accepted here, permanently.
"""

import hmac
import re
from dataclasses import dataclass

# `program.settings` is imported lazily inside the functions that need it. It
# pulls in RTN and the DB models, and the header parsing above is pure string
# work that the test suite exercises without either.

# `MediaBrowser Token="abc", Client="Android TV", Version="0.15"`.
# Values are quoted and may contain commas and spaces, so this pulls out
# quoted pairs rather than splitting the string.
_PAIR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """Who is calling, as far as the authorization header describes them.

    Only `token` is load-bearing. The rest is what Jellyfin dashboards show
    per device, and it is worth carrying because a "why is this transcoding"
    question is unanswerable without knowing which client asked.
    """

    token: str | None = None
    client: str | None = None
    device: str | None = None
    device_id: str | None = None
    version: str | None = None

    @property
    def label(self) -> str:
        return f"{self.client or 'unknown client'} on {self.device or 'unknown device'}"


def parse_authorization(raw: str | None) -> ClientIdentity:
    """Read a MediaBrowser-scheme header into its parts.

    Tolerant by design: an unparseable header yields an empty identity and a
    401 downstream, never an exception.
    """

    if not raw:
        return ClientIdentity()

    values = {key.lower(): value for key, value in _PAIR_RE.findall(raw)}

    return ClientIdentity(
        token=values.get("token") or None,
        client=values.get("client") or None,
        device=values.get("device") or None,
        device_id=values.get("deviceid") or None,
        version=values.get("version") or None,
    )


def identify(headers, query_params) -> ClientIdentity:
    """Pull the caller's identity out of whichever form they used.

    Order matters only in that an explicit token beats an implicit one; every
    form is otherwise equivalent. `headers` and `query_params` are the Starlette
    mappings, taken as parameters so this stays testable without a Request.
    """

    identity = parse_authorization(
        headers.get("authorization") or headers.get("x-emby-authorization")
    )

    if identity.token:
        return identity

    # The bare-token headers carry no client metadata, so keep whatever the
    # MediaBrowser header told us about the device and fill in just the token.
    token = (
        headers.get("x-emby-token")
        or headers.get("x-mediabrowser-token")
        # Discouraged, but some clients (and every "test it with curl") use it.
        or query_params.get("ApiKey")
        or query_params.get("api_key")
    )

    if not token:
        return identity

    return ClientIdentity(
        token=token,
        client=identity.client,
        device=identity.device,
        device_id=identity.device_id,
        version=identity.version,
    )


def is_valid_token(token: str | None) -> bool:
    """Constant-time check against the one secret this server has."""

    from program.settings import settings_manager

    api_key = settings_manager.settings.api_key

    if not token or not api_key:
        return False

    return hmac.compare_digest(token, api_key)


def check_password(username: str, password: str) -> bool:
    """Validate an AuthenticateByName attempt.

    The username is compared case-insensitively because clients let people type
    it freely and a TV keyboard makes a capital letter a real obstacle; the
    password is the API key and is compared exactly.
    """

    from program.settings import settings_manager

    settings = settings_manager.settings.jellyfin_server
    api_key = settings_manager.settings.api_key

    if not api_key:
        return False

    if (username or "").strip().lower() != settings.username.strip().lower():
        return False

    return hmac.compare_digest(password or "", api_key)


def issue_token() -> str:
    """The token handed to a client that just authenticated.

    It is the API key itself: the client necessarily supplied it as the
    password a moment ago, so this grants nothing new, and it means tokens
    survive restarts. See the module docstring.
    """

    from program.settings import settings_manager

    return settings_manager.settings.api_key
