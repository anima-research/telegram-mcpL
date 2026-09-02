"""Negotiated policy state — MCPL 0.5 §5.3 / §5.4 / §6.7.

The host computes the effective capability grant for this connection and
delivers it with `featureSets/update`. Until that exchange completes, every
capability-dependent behavior on this side is unavailable (§5.3); afterwards
`effectiveCapabilities` is the sole normative allowlist — absence is denial,
there is no unspecified state (§5.4).

Nothing here is ever widened by anything this server itself asserts. The
receipt we return is consequence testimony ("what we will do"), never a
claim about what we are entitled to (§6.7).

Ported from the pattern in dog_events_mcpl.py (the fleet's Python 0.5
reference) and discord-mcpl's `applyFeatureSetsUpdate` / `policyAnswered`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("telegram_mcp.mcpl")

# The single feature set this server declares (§6.1). Its `uses` must name
# every capability the set actually exercises — §6.4 derivation FAILS CLOSED
# on an incomplete or unrecognized list.
FS_NAME = "telegram.messaging"
FS_DESCRIPTION = (
    "Telegram as a user account: dialogs registered as channels, incoming "
    "messages pushed as channels/incoming, host publishes via channels/publish, "
    "typing indicator, plus the standard MCP tool surface."
)

CAP_TOOLS = "tools"
CAP_REGISTER = "channels.register"
CAP_PUBLISH = "channels.publish"
CAP_INCOMING = "channels.incoming"
CAP_LIFECYCLE = "channels.lifecycle"
CAP_TYPING = "channels.typing"

FS_USES: tuple[str, ...] = (
    CAP_TOOLS,
    CAP_REGISTER,
    CAP_PUBLISH,
    CAP_INCOMING,
    CAP_LIFECYCLE,
    CAP_TYPING,
)


def host_speaks_0_5(host_capabilities: dict[str, Any] | None) -> bool:
    """True when the host advertised MCPL >= 0.5 in `initialize`.

    A 0.5 host MUST send the initial policy as a Request (§5.3); a pre-0.5
    host never will, so callers use this to decide whether to wait for it.
    """
    if not host_capabilities:
        return False
    version = host_capabilities.get("version")
    if not isinstance(version, str):
        return False
    try:
        parts = tuple(int(p) for p in version.split(".")[:2])
    except ValueError:
        return False
    return parts >= (0, 5)


class PolicyState:
    """Per-connection grant + feature-set enablement, fail-closed."""

    def __init__(self) -> None:
        self.policy_received: bool = False
        self.effective_capabilities: set[str] = set()
        self.fs_enabled: bool = False
        self._ready = asyncio.Event()

    # -- authorization questions -------------------------------------------

    def granted(self, capability: str) -> bool:
        """Sole authorization question. Absence is denial (§5.4)."""
        return self.policy_received and capability in self.effective_capabilities

    @property
    def can_register(self) -> bool:
        return self.fs_enabled and self.granted(CAP_REGISTER)

    @property
    def can_push_incoming(self) -> bool:
        return self.fs_enabled and self.granted(CAP_INCOMING)

    async def wait_ready(self, timeout: float) -> bool:
        """Block until the initial policy Request has been answered.

        Returns False on timeout — the caller decides what "no policy" means
        (for a 0.5 host it means the privileged surface stays dark, §6.7).
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- featureSets/update -----------------------------------------------

    def apply(self, params: dict[str, Any], *, is_request: bool) -> dict[str, Any] | None:
        """Apply a `featureSets/update` frame.

        Request form (§5.3, §6.7): establishes/replaces the grant and returns
        the degradation receipt to send back. Notification form: narrow-only,
        never establishes readiness (§6.7 as pinned 2026-08-02); returns None.
        """
        if not is_request:
            self._apply_notification(params)
            return None

        effective = params.get("effectiveCapabilities")
        denied = params.get("deniedCapabilities") or []

        if not isinstance(effective, list) or not all(isinstance(c, str) for c in effective):
            # No usable allowlist → grant of nothing. Absent is denial (§5.3
            # field semantics) — we do NOT keep any previous wider grant.
            self._fail_closed(
                "featureSets/update carried no usable effectiveCapabilities array; "
                "§5.4 makes it the sole normative allowlist, so this server cannot "
                "derive a grant and stays MCP-only"
            )
            return {
                "accepted": False,
                "fallback": "mcp-only",
                "reason": "featureSets/update carried no usable effectiveCapabilities array (§5.3/§5.4)",
                "missingCapabilities": list(FS_USES),
            }

        effective_set = set(effective)
        overlap = effective_set & {c for c in denied if isinstance(c, str)}
        if overlap:
            # §5.4: a path in both lists → malformed → fail closed.
            self._fail_closed(f"malformed policy: {sorted(overlap)} in both effective and denied")
            return {
                "accepted": False,
                "fallback": "mcp-only",
                "reason": (
                    f"malformed policy: {sorted(overlap)} appear in both "
                    "effectiveCapabilities and deniedCapabilities (§5.4)"
                ),
            }

        self.effective_capabilities = effective_set
        self.policy_received = True

        # §6.4 derivation on our side too: a missing `uses` capability
        # disables the set. `enabled` present = allowlist; `disabled`
        # always subtracts; `deniedCapabilities` is diagnostic only.
        missing = [c for c in FS_USES if c not in effective_set]
        enabled_list = params.get("enabled")
        disabled_list = params.get("disabled") or []
        explicitly_disabled = FS_NAME in disabled_list
        selected = (not isinstance(enabled_list, list)) or (FS_NAME in enabled_list)
        self.fs_enabled = (not missing) and not explicitly_disabled and selected

        receipt: dict[str, Any] = {"accepted": True, "unavailableFeatures": [], "notes": []}
        if not self.fs_enabled:
            receipt["mode"] = "degraded"
            receipt["unavailableFeatures"].append(
                {
                    "featureSet": FS_NAME,
                    "missingCapabilities": missing,
                    "effect": "disabled",
                }
            )
        # Consequence testimony (§6.7): what degrades, never what we are owed.
        if CAP_INCOMING not in effective_set:
            receipt["notes"].append(
                "Without channels.incoming no Telegram message reaches the host; "
                "the agent can still send via tools but is never woken by Telegram."
            )
        if CAP_REGISTER not in effective_set:
            receipt["notes"].append(
                "Without channels.register dialogs are not registered as channels; "
                "channels/publish and locus routing to Telegram will not resolve."
            )

        log.info(
            "featureSets/update Request applied: %d capabilities, %s %s",
            len(effective_set),
            FS_NAME,
            "enabled" if self.fs_enabled else "DISABLED",
        )
        self._ready.set()
        return receipt

    def _apply_notification(self, params: dict[str, Any]) -> None:
        """Notification form: reductions honoured, everything else ignored."""
        if not self.policy_received:
            log.warning(
                "featureSets/update arrived as a Notification before the initial "
                "Request-form policy exchange; §6.7: a Notification cannot establish "
                "a ready state — ignored"
            )
            return
        disabled_list = params.get("disabled") or []
        if FS_NAME in disabled_list and self.fs_enabled:
            self.fs_enabled = False
            log.info(
                "featureSets/update Notification applied as a narrowing: %s disabled", FS_NAME
            )
        else:
            log.info(
                "featureSets/update Notification carried no narrowing for this server; "
                "any widening in it is ignored (§6.7)"
            )

    def _fail_closed(self, why: str) -> None:
        self.effective_capabilities = set()
        self.fs_enabled = False
        self.policy_received = False
        log.warning("featureSets/update rejected — failing closed: %s", why)
        # Wake any waiter so registration does not hang on a refused policy;
        # `granted()` stays False so nothing privileged runs.
        self._ready.set()
