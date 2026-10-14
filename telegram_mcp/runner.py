"""Application entrypoints for the Telegram MCP server."""

from telegram_mcp.install_guard import UnsafeInstallationError, assert_safe_distribution

try:
    assert_safe_distribution()
except UnsafeInstallationError as exc:
    raise SystemExit(str(exc)) from None

import sys
from telethon.errors import AuthKeyDuplicatedError, BotMethodInvalidError

# Ensure sys.stderr is reconfigured for UTF-8 on Windows where possible
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass

from telegram_mcp import runtime as _runtime
from telegram_mcp import transcription as _transcription
from telegram_mcp.runtime import *
from telegram_mcp.singleton import (
    DEFAULT_GRACE_SECONDS,
    SessionLock,
    SessionLockError,
    session_identity,
)
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


# Populated as each account's session lock is acquired; released in _main's
# finally block so a lock is never held past this process's lifetime.
_session_locks: dict[str, SessionLock] = {}


def _lock_grace_seconds() -> float:
    raw = os.getenv("TELEGRAM_LOCK_GRACE_SECONDS")
    if not raw:
        return DEFAULT_GRACE_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_GRACE_SECONDS


_SESSION_LOCK_MODES = ("exclusive", "shared")


def _session_lock_shared() -> bool:
    """``TELEGRAM_SESSION_LOCK``: exclusive (default) | shared.

    ``exclusive`` refuses to start while another instance on this host holds
    the same session. ``shared`` takes the lock in shared mode instead, so any
    number of instances on this host can use one session -- safe only when
    they all reach Telegram from the same IP, since Telegram invalidates a
    session it sees from two IPs at once. Shared and exclusive instances never
    overlap, so the default keeps its guarantee.
    """
    raw = (os.getenv("TELEGRAM_SESSION_LOCK") or "exclusive").strip().lower()
    if raw not in _SESSION_LOCK_MODES:
        accepted = ", ".join(_SESSION_LOCK_MODES)
        raise SystemExit(f"Invalid TELEGRAM_SESSION_LOCK '{raw}'. Expected one of: {accepted}.")
    return raw == "shared"


def _normalize_username(value) -> str:
    return (value or "").strip().lstrip("@").casefold()


def _expected_username(label: str) -> str:
    """TELEGRAM_EXPECTED_USERNAME_<LABEL>, else TELEGRAM_EXPECTED_USERNAME; "" if unset."""
    raw = os.getenv(f"TELEGRAM_EXPECTED_USERNAME_{label.upper()}") or os.getenv(
        "TELEGRAM_EXPECTED_USERNAME"
    )
    return _normalize_username(raw)


async def _verify_expected_username(label: str, client) -> None:
    """Refuse to serve a session logged in to a different account."""
    expected = _expected_username(label)
    if not expected:
        return

    me = await client.get_me()
    usernames = {_normalize_username(getattr(me, "username", None))}
    for entry in getattr(me, "usernames", None) or []:
        if getattr(entry, "active", False):
            usernames.add(_normalize_username(getattr(entry, "username", None)))
    usernames.discard("")

    if expected in usernames:
        return

    try:
        await client.disconnect()
    except Exception:
        pass
    raise RuntimeError(
        f"Telegram client '{label}' is logged in to a different account than "
        f"TELEGRAM_EXPECTED_USERNAME_{label.upper()} / TELEGRAM_EXPECTED_USERNAME expects."
    )


async def _sign_in_with_bot_token(label, client) -> bool:
    """Sign an unauthorized session in as a bot when a token is configured.

    TELEGRAM_BOT_TOKEN_<LABEL>, else TELEGRAM_BOT_TOKEN for the default
    account. With a file session the login is kept, so this runs once.
    """
    from telegram_mcp.mcpl.access import bot_token_for

    token = bot_token_for(label)
    if not token:
        return False
    await client.sign_in(bot_token=token)
    print(f"Telegram client '{label}' signed in as a bot.", file=sys.stderr)
    return True


async def _connect_authorized_client(label, client) -> None:
    # First, prevent our own duplicate-spawn case outright: a per-session lock
    # means a second instance of this server never even attempts to connect
    # while another instance already holds the same session (see
    # telegram_mcp/singleton.py for why and how). The lock only sees processes
    # on this host; TELEGRAM_SESSION_LOCK=shared lets those share one session
    # where they all reach Telegram from a single IP.
    lock = SessionLock(label, session_identity(client))
    await asyncio.to_thread(
        lock.acquire, grace_seconds=_lock_grace_seconds(), shared=_session_lock_shared()
    )
    _session_locks[label] = lock

    # Once we hold the lock, still tolerate a transient AuthKeyDuplicatedError
    # from Telegram itself (e.g. the same session briefly seen from two IPs
    # during a VPN reconnect) with a bounded retry, since that's not caused by
    # a second instance of this server and a blip shouldn't take the server
    # down. Give each concurrent client its own session (TELEGRAM_SESSION_STRINGS
    # pool or TELEGRAM_SESSION_STRING_<LABEL>) to avoid the collision entirely.
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        try:
            await client.connect()
            break
        except AuthKeyDuplicatedError:
            if attempt >= max_attempts:
                raise
            delay = min(2**attempt, 15)
            print(
                f"AuthKeyDuplicatedError connecting '{label}' (attempt "
                f"{attempt}/{max_attempts}): session in use from another IP. "
                f"Retrying in {delay}s. If this persists, give each concurrent "
                "client its own session via TELEGRAM_SESSION_STRINGS or "
                "TELEGRAM_SESSION_STRING_<LABEL>.",
                file=sys.stderr,
            )
            try:
                await client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(delay)

    if await client.is_user_authorized():
        await _verify_expected_username(label, client)
        return

    if await _sign_in_with_bot_token(label, client):
        await _verify_expected_username(label, client)
        return

    raise RuntimeError(
        f"Telegram client '{label}' is not authorized. Interactive phone login "
        "is disabled for the MCP server because it runs over stdio. Generate a "
        "session string with `uv run session_string_generator.py`, then set "
        "TELEGRAM_SESSION_STRING or TELEGRAM_SESSION_STRING_<LABEL> in .env. "
        "For existing file sessions, run the login outside the MCP server first."
    )


def _configure_transport_security() -> None:
    """Wire MCP_ALLOWED_HOSTS/MCP_ALLOWED_ORIGINS into FastMCP's DNS-rebinding
    protection, e.g. when the server sits behind a reverse proxy on a public
    domain instead of only being reached via 127.0.0.1/localhost.
    """
    raw_hosts = os.getenv("MCP_ALLOWED_HOSTS", "")
    allowed_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
    if not allowed_hosts:
        return

    from mcp.server.transport_security import TransportSecuritySettings

    raw_origins = os.getenv("MCP_ALLOWED_ORIGINS", "")
    allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


async def _serve(transport: str) -> None:
    """Run the MCP server on the selected transport.

    HTTP transports let one long-lived process hold a single shared Telegram
    connection while multiple local MCP clients connect over HTTP, instead of
    each client spawning its own Telethon session (which Telegram
    throttles/flags). "http" is streamable HTTP — the current MCP transport
    that Claude Code (`--transport http`) and Codex (`--url`) speak natively;
    "sse" is kept for clients that only support the legacy SSE transport.
    """
    if transport in ("http", "sse"):
        mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8765"))
        _configure_transport_security()
        if transport == "http":
            await mcp.run_streamable_http_async()
        else:
            await mcp.run_sse_async()
    else:
        # Use the asynchronous entrypoint instead of mcp.run()
        await mcp.run_stdio_async()


async def _serve_mcpl_stdio() -> None:
    """Serve over the MCPL-aware stdio transport.

    Advertises experimental.mcpl in the initialize handshake, registers
    Telegram dialogs as MCPL channels once the host signals ready, and exposes
    channels/publish so the host can send messages through us. Plain MCP hosts
    see an ordinary stdio server.
    """
    policy = PolicyState()
    on_ready = _build_on_ready_hook(policy)
    dispatcher = _build_dispatcher(policy)
    await run_stdio_with_mcpl(mcp, dispatcher=dispatcher, on_ready=on_ready)


async def _main() -> None:
    try:
        labels = ", ".join(clients.keys())
        print(f"Starting {len(clients)} Telegram client(s) ({labels})...", file=sys.stderr)
        await asyncio.gather(
            *(_connect_authorized_client(label, cl) for label, cl in clients.items())
        )

        # Warm entity caches — StringSession has no persistent cache,
        # so fetch all dialogs once per client to populate them.
        # Runs in background: blocking startup on this (e.g. under a
        # GetDialogsRequest flood wait) makes MCP clients time out, and
        # resolve_entity() re-warms the cache on miss anyway.
        print("Warming entity caches (background)...", file=sys.stderr)

        async def _warm_caches() -> None:
            async def _warm_client(label: str, cl: TelegramClient) -> None:
                try:
                    await cl.get_dialogs()
                except BotMethodInvalidError:
                    print(
                        f"Skipping entity cache pre-warm for bot client '{label}' (dialogs restricted for bots).",
                        file=sys.stderr,
                    )
                except Exception as exc:
                    print(f"Entity cache warm failed for '{label}': {exc}", file=sys.stderr)

            try:
                await asyncio.gather(*(_warm_client(label, cl) for label, cl in clients.items()))
                print("Entity caches warmed.", file=sys.stderr)
            except Exception as warm_exc:
                print(f"Entity cache warm failed: {warm_exc}", file=sys.stderr)

        warm_task = asyncio.create_task(_warm_caches())

        transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

        # Validate transport mode
        VALID_TRANSPORTS = ("stdio", "http", "sse")
        if transport not in VALID_TRANSPORTS:
            accepted = ", ".join(VALID_TRANSPORTS)
            print(
                f"Invalid MCP_TRANSPORT '{transport}'. Expected one of: {accepted}.",
                file=sys.stderr,
            )
            sys.exit(1)

        # Startup log with host:port for HTTP/SSE transports
        if transport in ("http", "sse"):
            host = os.getenv("MCP_HOST", "127.0.0.1")
            port = os.getenv("MCP_PORT", "8765")
            print(
                f"Telegram client(s) started ({labels}). Running MCP server ({transport}) on {host}:{port}...",
                file=sys.stderr,
            )
        else:
            print(
                f"Telegram client(s) started ({labels}). Running MCP server ({transport})...",
                file=sys.stderr,
            )

        if transport == "stdio":
            await _serve_mcpl_stdio()
        else:
            # MCPL rides on the stdio framing only; http/sse serve plain MCP.
            await _serve(transport)
    except Exception as e:
        print(f"Error starting client: {e}", file=sys.stderr)
        if isinstance(e, sqlite3.OperationalError) and "database is locked" in str(e):
            print(
                "Database lock detected. Please ensure no other instances are running.",
                file=sys.stderr,
            )
        elif isinstance(e, SessionLockError):
            print(
                "Another instance of this MCP server already holds this Telegram "
                "session (e.g. the client restarted the connector without the old "
                "process exiting yet). This instance is exiting instead of "
                "connecting a second time, which would risk Telegram invalidating "
                "the session for both. Retry once the other instance is gone, or "
                "set TELEGRAM_SESSION_LOCK=shared if several instances on this "
                "host are meant to share one session (safe when they all reach "
                "Telegram from the same IP).",
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
        for lock in _session_locks.values():
            lock.release()
        _session_locks.clear()


def main() -> None:
    _configure_allowed_roots_from_cli(sys.argv[1:])
    # Apply CLI transport/host/port overrides to environment (runtime sets globals)
    transport = _runtime._CLI_TRANSPORT or "stdio"
    host = _runtime._CLI_HOST
    port = _runtime._CLI_PORT

    if transport != "stdio":
        os.environ["MCP_TRANSPORT"] = transport
    if host:
        os.environ["MCP_HOST"] = host
    if port is not None:
        os.environ["MCP_PORT"] = str(port)
    # Before _apply_exposed_tools_mode(): that prunes non-exposed tools from
    # the tool manager, and the extension overrides validate tool names
    # against that same manager. Narrowing send_file's extensions while
    # send_file is not exposed is a valid configuration, so the name check
    # has to see the full tool set.
    _runtime._apply_file_extension_overrides()
    hidden, excluded = _runtime._apply_exposed_tools_mode()
    if hidden:
        # These names are the menu for the "+" allowlist; without this line the
        # only way to find them is reading the tool annotations in the source.
        print(
            f"TELEGRAM_EXPOSED_TOOLS hides {len(hidden)} tool(s); list any of them "
            f"after '+' to expose it: {', '.join(sorted(hidden))}",
            file=sys.stderr,
        )
    if excluded:
        print(
            f"TELEGRAM_EXPOSED_TOOLS excludes {len(excluded)} tool(s) listed "
            f"after '-': {', '.join(sorted(excluded))}",
            file=sys.stderr,
        )
    _transcription.validate_transcription_config()
    _session_lock_shared()  # fail loudly at startup on a bad toggle
    asyncio.run(_main())


if __name__ == "__main__":
    main()
