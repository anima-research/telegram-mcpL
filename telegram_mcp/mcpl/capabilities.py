"""MCPL capability advertisement (the server manifest, §5.1).

Returns the dict that goes under `experimental.mcpl` in the server's
`initialize` response. Advertisement mirrors the §6.2 capability paths leaf
by leaf: `true` advertises the leaf, `false`/absent advertises nothing. The
host computes the effective grant from this (§5.4) — advertisement is an
input, never an authorization.

This manifest is fixed for the life of the connection (no `revision`,
§17.10); `mcpl/manifest` answers with the same object.
"""

from __future__ import annotations

from typing import Any

from . import MCPL_VERSION
from .policy import (
    FS_DESCRIPTION,
    FS_NAME,
    FS_TRANSCRIPTS_DESCRIPTION,
    FS_TRANSCRIPTS_NAME,
    FS_TRANSCRIPTS_USES,
    FS_USES,
)


def build_mcpl_capabilities() -> dict[str, Any]:
    """Return the dict to inject as `experimental.mcpl` on `initialize`."""
    return {
        "version": MCPL_VERSION,
        # Telegram traffic arrives as channels/incoming (§14); push/event
        # carries only late transcripts (telegram.transcripts).
        "pushEvents": True,
        "channels": {
            "register": True,
            "publish": True,
            "incoming": True,
            "lifecycle": True,
            "typing": True,
            "streaming": False,
            "acknowledge": False,
        },
        # §6.1 Record form. `uses` must list every capability the set
        # exercises — §6.4 derivation fails closed on omissions.
        "featureSets": {
            FS_NAME: {
                "description": FS_DESCRIPTION,
                "uses": list(FS_USES),
            },
            FS_TRANSCRIPTS_NAME: {
                "description": FS_TRANSCRIPTS_DESCRIPTION,
                "uses": list(FS_TRANSCRIPTS_USES),
            },
        },
    }


def build_experimental_capabilities() -> dict[str, dict[str, Any]]:
    """Wrap our capabilities for `Server.create_initialization_options`.

    The mcp SDK takes `experimental_capabilities: dict[str, dict[str, Any]]`
    where the top-level keys are namespaces. Ours is `mcpl`.
    """
    return {"mcpl": build_mcpl_capabilities()}
