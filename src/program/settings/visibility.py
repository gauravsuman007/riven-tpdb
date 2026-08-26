"""Which settings this fork exposes.

Upstream's settings models are kept intact so that upstream changes to them
merge cleanly -- deleting the provider models outright was worth ~230 lines of
permanent conflict surface. The providers this fork cannot use are hidden from
the settings schema instead, which is what the UI renders from.

Hidden here means "not shown and not editable". The values still exist on the
model and round-trip untouched: the settings form submits the value snapshot
it was given, and `/settings/set/all` merges rather than replaces, so a hidden
section is preserved rather than reset.

To re-enable a provider, delete its entry below -- but check first that it can
actually serve adult content. Only Prowlarr and Jackett can; the Stremio-style
scrapers address content by IMDb id, which TPDB titles do not have.
"""

from copy import deepcopy
from typing import Any

# Nested sections to hide, keyed by the top-level settings key.
HIDDEN_SECTIONS: dict[str, frozenset[str]] = {
    # Mainstream request/list providers. None of them can produce an item
    # carrying a TPDB id, which is the only thing this fork's indexer resolves.
    "content": frozenset(
        {"overseerr", "plex_watchlist", "mdblist", "listrr", "trakt"}
    ),
    # Stremio-style scrapers: they look content up by IMDb id, so they can only
    # ever return nothing here.
    "scraping": frozenset(
        {"torrentio", "orionoid", "mediafusion", "comet", "rarbg", "aiostreams"}
    ),
    # Provider wiring, not a user setting. `tailscale.auth_key` is the field
    # that matters and it already has a dedicated write path: the VPN tab's
    # control panel saves it as a side effect of clicking "Connect with key".
    # Also rendering it here gave the page two auth-key inputs with no way to
    # tell which one was live, and the failure mode was worse than confusing:
    # saving a key through the generic form (not the panel) set
    # tailscale.auth_key without ever calling connect(), so /vpn/connect's
    # fallback to the stored key made every later "Log in" attempt silently
    # try key auth instead of generating a login URL -- the button the user
    # was looking for never had a reason to appear. `socket_path` and
    # `proxy_url` are container wiring meant to match docker-compose, the same
    # reasoning that keeps RIVEN_* infra out of the settings UI elsewhere.
    "vpn": frozenset({"tailscale"}),
}


def _definition_for(schema: dict[str, Any], top_level_key: str) -> dict[str, Any] | None:
    """Resolve the `$defs` entry backing a top-level settings key."""

    prop = schema.get("properties", {}).get(top_level_key)

    if not isinstance(prop, dict):
        return None

    # `/settings/schema/keys` builds each field with its own TypeAdapter, which
    # can inline the sub-model rather than referencing `$defs`.
    if isinstance(prop.get("properties"), dict):
        return prop

    ref = prop.get("$ref")

    # Pydantic emits a bare `$ref` for a required sub-model and wraps it in
    # `allOf`/`anyOf` when the field carries extra metadata.
    if not ref:
        for combinator in ("allOf", "anyOf", "oneOf"):
            for entry in prop.get(combinator, []):
                if isinstance(entry, dict) and entry.get("$ref"):
                    ref = entry["$ref"]
                    break
            if ref:
                break

    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return None

    return schema.get("$defs", {}).get(ref.removeprefix("#/$defs/"))


def prune_settings_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return `schema` without the sections this fork cannot use.

    The input is not mutated -- pydantic may hand back a cached schema object.
    """

    pruned = deepcopy(schema)

    for top_level_key, hidden in HIDDEN_SECTIONS.items():
        definition = _definition_for(pruned, top_level_key)

        if not definition:
            continue

        properties = definition.get("properties")

        if not isinstance(properties, dict):
            continue

        for name in hidden:
            properties.pop(name, None)

        if isinstance(required := definition.get("required"), list):
            definition["required"] = [n for n in required if n not in hidden]

    return pruned
