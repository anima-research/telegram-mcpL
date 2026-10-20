"""push/event emission for late transcripts (feature set telegram.transcripts).

The transport and the policy exist only on an MCPL connection, after the
handshake; `bind` stores them from the on_ready hook. Without them (plain MCP
host, or before ready) nothing is pushed and the caller falls back to polling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .policy import FS_TRANSCRIPTS_NAME, PolicyState
from .transport import McplTransport

log = logging.getLogger("telegram_mcp.mcpl")

PUSH_TIMEOUT_SECONDS = 15.0

_transport: Optional[McplTransport] = None
_policy: Optional[PolicyState] = None


def bind(transport: Optional[McplTransport], policy: Optional[PolicyState]) -> None:
    global _transport, _policy
    _transport, _policy = transport, policy


def available() -> bool:
    return _transport is not None and _policy is not None and _policy.can_push_transcripts


async def push_text(event_id: str, text: str, origin: dict[str, Any]) -> bool:
    """Send one push/event with a text block; True when the host accepted it."""
    if not available():
        return False
    params = {
        "featureSet": FS_TRANSCRIPTS_NAME,
        "eventId": event_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "payload": {"content": [{"type": "text", "text": text}]},
    }
    try:
        result = await asyncio.wait_for(
            _transport.request("push/event", params), PUSH_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 — a lost push must not crash the task
        log.warning("push/event %s failed: %s", event_id, exc)
        return False
    accepted = isinstance(result, dict) and result.get("accepted") is True
    if not accepted:
        log.warning("push/event %s not accepted: %s", event_id, result)
    return accepted
