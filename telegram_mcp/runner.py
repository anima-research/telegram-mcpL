"""Application entrypoints for the Telegram MCP server."""

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

from telegram_mcp.runtime import *
import telegram_mcp.tools  # noqa: F401 - registers MCP tools via decorators
from telegram_mcp.mcpl.capabilities import build_mcpl_capabilities
from telegram_mcp.mcpl.channels import enumerate_channels
from telegram_mcp.mcpl.dispatcher import McplDispatcher
from telegram_mcp.mcpl.events import attach_event_handlers
from telegram_mcp.mcpl.policy import PolicyState
from telegram_mcp.mcpl.ready import make_on_ready
from telegram_mcp.mcpl.handlers import (
    make_close_handler,
    make_list_handler,
    make_open_handler,
    make_publish_handler,
    make_typing_handler,
)
from telegram_mcp.mcpl.transport import run_stdio_with_mcpl


def _build_dispatcher(policy: PolicyState) -> McplDispatcher:
    """Construct the MCPL dispatcher with all server-side handlers wired in."""
    dispatcher = McplDispatcher()

    # MCPL 0.5 §5.3/§6.7 — the host's policy exchange. Request form returns the
    # degradation receipt; Notification form may only narrow.
    async def handle_feature_sets_update(params, is_request: bool):
        return policy.apply(params, is_request=is_request)

    dispatcher.register_frame_aware("featureSets/update", handle_feature_sets_update)

    # §17.4 — the complete current manifest, same shape initialize carries.
    async def handle_manifest(params):
        return build_mcpl_capabilities()

    dispatcher.register("mcpl/manifest", handle_manifest)
    dispatcher.register(
        "channels/publish",
        make_publish_handler(
            clients,
            resolve_entity_fn=resolve_entity,
            ensure_connected_fn=ensure_connected,
        ),
    )
    dispatcher.register("channels/list", make_list_handler(clients))
    dispatcher.register("channels/open", make_open_handler())
    dispatcher.register("channels/close", make_close_handler())
    dispatcher.register(
        "channels/typing",
        make_typing_handler(
            clients,
            resolve_entity_fn=resolve_entity,
            ensure_connected_fn=ensure_connected,
        ),
    )
    return dispatcher


def _build_on_ready_hook(policy: PolicyState):
    """After the host signals it's initialized: wait for the 0.5 policy
    receipt, register Telegram dialogs as MCPL channels (if granted), and
    attach the Telethon event handlers that translate NewMessage events into
    `channels/incoming` pushes gated on the live grant. See mcpl/ready.py.
    """
    return make_on_ready(
        clients,
        policy=policy,
        enumerate_channels=enumerate_channels,
        attach_event_handlers=attach_event_handlers,
    )


async def _main() -> None:
    try:
        labels = ", ".join(clients.keys())
        print(f"Starting {len(clients)} Telegram client(s) ({labels})...", file=sys.stderr)
        await asyncio.gather(*(cl.start() for cl in clients.values()))

        # Warm entity caches — StringSession has no persistent cache,
        # so fetch all dialogs once per client to populate them
        print("Warming entity caches...", file=sys.stderr)
        await asyncio.gather(*(cl.get_dialogs() for cl in clients.values()))

        print(f"Telegram client(s) started ({labels}). Running MCP server...", file=sys.stderr)
        # MCPL-aware stdio runner — advertises experimental.mcpl in the
        # initialize handshake, registers Telegram dialogs as MCPL channels
        # once the host signals ready, and exposes channels/publish so the
        # host can send messages through us.
        policy = PolicyState()
        on_ready = _build_on_ready_hook(policy)
        dispatcher = _build_dispatcher(policy)
        await run_stdio_with_mcpl(mcp, dispatcher=dispatcher, on_ready=on_ready)
    except Exception as e:
        print(f"Error starting client: {e}", file=sys.stderr)
        if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
            print(
                "Database lock detected. Please ensure no other instances are running.",
                file=sys.stderr,
            )
        sys.exit(1)
    finally:
        try:
            await asyncio.gather(
                *(cl.disconnect() for cl in clients.values()), return_exceptions=True
            )
        except Exception:
            pass


def main() -> None:
    _configure_allowed_roots_from_cli(sys.argv[1:])
    nest_asyncio.apply()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
