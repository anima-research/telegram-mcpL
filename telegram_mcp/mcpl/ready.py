"""Post-handshake startup: register channels + attach handlers, behind policy.

MCPL 0.5 §5.3: a host MUST reject inbound privileged methods until the
initial `featureSets/update` Request has been answered, and §6.7 says an
unanswered expansion never activates. So `channels/register` — a privileged
server→host Request — must not be sent before the policy exchange completes,
or the host rejects it (-32002) and the connection ends up registered
nowhere. discord-mcpl hit exactly this deadlock (90f869f); the rule that
shipped there: nothing runs between `initialize` and the read loop, and
registration waits behind a `policyAnswered` gate with a bounded grace for
pre-0.5 hosts.

This module is the pure, testable core of that rule; `runner.py` binds it to
the live Telethon clients.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .policy import PolicyState, host_speaks_0_5
from .transport import McplTransport

log = logging.getLogger("telegram_mcp.mcpl")

# How long to wait for the host's initial policy Request before giving up on
# the privileged surface. The host's own timeout for the round-trip is 15s
# (agent-framework `sendFeatureSetsUpdateRequest`); 20s leaves room for a
# slow start on either side without hanging forever.
POLICY_WAIT_SECONDS = 20.0

EnumerateFn = Callable[[Any, str], Awaitable[list[dict[str, Any]]]]
AttachFn = Callable[..., Awaitable[None]]


def make_on_ready(
    clients: dict[str, Any],
    *,
    policy: PolicyState,
    enumerate_channels: EnumerateFn,
    attach_event_handlers: AttachFn,
    policy_wait_seconds: float = POLICY_WAIT_SECONDS,
) -> Callable[[McplTransport], Awaitable[None]]:
    """Build the transport's on_ready hook.

    Order on a 0.5 host: wait for the policy receipt → if `channels.register`
    is granted, enumerate dialogs and send `channels/register` → attach the
    Telethon handlers (which gate every push on the live grant). On a pre-0.5
    host (or one that never identified itself) there is no policy exchange to
    wait for; behave as before.
    """

    async def on_ready(transport: McplTransport) -> None:
        if host_speaks_0_5(transport.host_capabilities):
            answered = await policy.wait_ready(policy_wait_seconds)
            if not answered:
                log.warning(
                    "host advertised MCPL 0.5 but sent no featureSets/update within %.0fs; "
                    "privileged surface stays dark (§6.7 unanswered expansion). "
                    "MCP tools keep working.",
                    policy_wait_seconds,
                )
                return
            if not policy.can_register:
                log.warning(
                    "channels.register not granted (fs_enabled=%s) — skipping "
                    "channels/register; Telegram dialogs are not channels on this host",
                    policy.fs_enabled,
                )
                # Still attach handlers: they gate on the live grant, so a later
                # widening Request activates pushes without a restart.
                await _attach_all(clients, transport, policy, attach_event_handlers)
                return
        else:
            log.info("host is pre-0.5 MCPL (or unversioned) — no policy exchange to await")

        all_channels: list[dict[str, Any]] = []
        for label, cl in clients.items():
            try:
                all_channels.extend(await enumerate_channels(cl, label))
            except Exception as exc:  # noqa: BLE001 — never block the agent on enumeration
                log.error("Failed to enumerate channels for account '%s': %s", label, exc)
        await transport.send_notification("channels/register", {"channels": all_channels})
        log.info("Registered %d MCPL channels with host", len(all_channels))

        await _attach_all(clients, transport, policy, attach_event_handlers)

    return on_ready


async def _attach_all(
    clients: dict[str, Any],
    transport: McplTransport,
    policy: PolicyState,
    attach_event_handlers: AttachFn,
) -> None:
    for label, cl in clients.items():
        try:
            await attach_event_handlers(
                cl, account_label=label, transport=transport, policy=policy
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to attach event handlers for account '%s': %s", label, exc)
