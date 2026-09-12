"""Add-ons: self-contained features the host loads from a folder.

See `contract.py` for what an add-on is, `database.py` for why each one owns
a Postgres schema, and `loader.py` for how failures stay visible.
"""

from program.addons.contract import (
    HOST_API_VERSION,
    Addon,
    AddonManifest,
    AddonNav,
    AddonTv,
)
from program.addons.loader import AddonRegistry, LoadedAddon, registry


__all__ = [
    "Addon",
    "AddonManifest",
    "AddonNav",
    "AddonTv",
    "AddonRegistry",
    "HOST_API_VERSION",
    "LoadedAddon",
    "registry",
]
