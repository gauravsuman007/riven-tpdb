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
        {
            "overseerr", "plex_watchlist", "mdblist", "listrr", "trakt",
            # One level deeper: the OnlyFans tab's per-row toggle owns
            # `disabled`, so rendering a raw list editor in the generic form
            # would be a second write path to the same key -- the trap
            # `tailscale.auth_key` caused. `plugin_dir` is deliberately NOT
            # hidden: like `results_per_site` it has no other write path.
            "onlyfans.disabled",
        }
    ),
    # Stremio-style scrapers: they look content up by IMDb id, so they can only
    # ever return nothing here.
    #
    # `dubbed_anime_only` is upstream's anime filter. There is no anime in an
    # adult-only library, so the toggle can only ever do harm: turning it on
    # restricts every scrape to a language tag none of these releases carry.
    "scraping": frozenset(
        {
            "torrentio", "orionoid", "mediafusion", "comet", "rarbg",
            "aiostreams", "dubbed_anime_only",
        }
    ),
    # Upstream's other two media servers. This fork's library is a debrid VFS,
    # which Plex and Emby cannot scan the way Jellyfin can -- and the fork's
    # own answer to that problem is `jellyfin_server`, on the same tab, where
    # Riven *is* the server. Leaving all three visible presented two dead
    # options beside the one that works.
    "updaters": frozenset({"plex", "emby"}),
    # Upstream ships movie/show/season/episode here. This fork only ever
    # produces movies, so the list is a choice between one real value and
    # three that match nothing.
    "notifications": frozenset({"on_item_type"}),
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
    # NOTE the asymmetry: `gluetun` is deliberately NOT hidden. The reason
    # `tailscale` is hidden is the competing write path, and gluetun has none
    # -- there is no "connect with key" button saving its api_key as a side
    # effect, because Gluetun has no interactive login to attach one to. Its
    # control_url/proxy_url/api_key are only ever written through the generic
    # form, so hiding them would leave no way to set them at all.
    "vpn": frozenset({"tailscale"}),
    # `disabled` has a dedicated write path: the Plugins tab's per-row toggle.
    # Rendering it in the generic form too would repeat the exact
    # two-write-paths trap `tailscale.auth_key` caused above -- a raw list-of-
    # strings editor saved through the generic form and the toggle's
    # `/settings/set/direct_scraping.disabled` both writing the same key with
    # no way to tell which one is current.
    # `site_order` joins `disabled` here for the identical reason: the
    # Plugins tab's reorder controls own it, and a raw list-of-strings editor
    # in the generic form would be a second write path to the same value.
    # `results_per_site` is deliberately NOT hidden -- it has no other write
    # path, so hiding it would leave no way to set it at all.
    "direct_scraping": frozenset({"disabled", "site_order"}),
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


def _resolve(schema: dict[str, Any], prop: Any) -> dict[str, Any] | None:
    """The definition backing one property, inlined or behind a `$ref`."""

    if not isinstance(prop, dict):
        return None

    if isinstance(prop.get("properties"), dict):
        return prop

    ref = prop.get("$ref")

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


def _pop_nested(
    schema: dict[str, Any], properties: dict[str, Any], dotted: str
) -> None:
    """Remove a field addressed as `section.field` (or deeper).

    Walks the `$defs` chain rather than assuming the sub-model is inlined,
    because pydantic emits either shape depending on how the field is declared.
    Missing at any step is a no-op: a hidden field that no longer exists should
    not break rendering the rest of the form.
    """

    *path, leaf = dotted.split(".")
    definition: dict[str, Any] | None = {"properties": properties}

    for step in path:
        if definition is None:
            return

        nested = definition.get("properties", {}).get(step)
        definition = _resolve(schema, nested)

    if definition is None:
        return

    nested_properties = definition.get("properties")

    if isinstance(nested_properties, dict):
        nested_properties.pop(leaf, None)

    if isinstance(required := definition.get("required"), list):
        definition["required"] = [name for name in required if name != leaf]


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
            # A dotted name hides a field inside a nested section rather than
            # the section itself -- `onlyfans.disabled` hides one field, where
            # a bare `onlyfans` would hide the whole tab's settings.
            if "." in name:
                _pop_nested(pruned, properties, name)
                continue

            properties.pop(name, None)

        if isinstance(required := definition.get("required"), list):
            definition["required"] = [n for n in required if n not in hidden]

    return pruned
