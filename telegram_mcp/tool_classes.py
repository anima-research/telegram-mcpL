"""MCPL tool classes for every tool this server registers.

Implements MCPL RFC-008 (tool classes):
https://github.com/anima-research/mcpl/blob/main/RFC-008-tool-classes.md

Each tool in ``tools/list`` carries ``_meta: {"mcpl/class": [<classes>]}``, a hint
hosts use as a policy key, for example to decide what tool-lifecycle observers may
see about a call. A tool may have several classes; hosts apply the union of their
restrictions. A tool with no class (in ``UNCLASSED``, or missing from
``TOOL_CLASSES``) is handled as the most restrictive case, so leaving a tool
unclassed is always safe. Classing it wrongly is not.

The comms rule: this server logs in as a Telegram *user* account, so chats,
messages, drafts, read receipts, reactions, contacts, participants and profiles all
belong to real people. Any tool that sends, carries or reads people's messages or
details is ``comms``, whatever else it also is, and hosts never share the
arguments of a ``comms`` tool with observers. When in doubt, add ``comms``.

Adding a tool: list it here (or in ``UNCLASSED``). tests/test_tool_classes.py
fails for any registered tool that is in neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

MCPL_CLASS_KEY = "mcpl/class"

# RFC-008 vocabulary. New classes come only from amendments to the RFC.
TOOL_CLASS_VOCABULARY = frozenset(
    {
        "comms",
        "memory",
        "notes",
        "files",
        "shell",
        "web",
        "computer",
        "media",
        "body",
        "control",
    }
)

_COMMS = ("comms",)
_COMMS_FILES = ("comms", "files")
_COMMS_CONTROL = ("comms", "control")
_CONTROL = ("control",)

TOOL_CLASSES: dict[str, tuple[str, ...]] = {
    # --- comms: send, edit, delete or react to people's messages -------------
    "send_message": _COMMS,
    "reply_to_message": _COMMS,
    "send_scheduled_message": _COMMS,
    "delete_scheduled_message": _COMMS,
    "edit_message": _COMMS,
    "delete_message": _COMMS,
    "delete_messages_bulk": _COMMS,
    "delete_chat_history": _COMMS,
    "forward_message": _COMMS,
    "pin_message": _COMMS,
    "unpin_message": _COMMS,
    "unpin_all_messages": _COMMS,
    "create_poll": _COMMS,
    "send_reaction": _COMMS,
    "remove_reaction": _COMMS,
    "send_gif": _COMMS,
    "send_contact": _COMMS,
    "press_inline_button": _COMMS,
    "mark_as_read": _COMMS,
    "save_draft": _COMMS,
    "clear_draft": _COMMS,
    # --- comms: read people's messages and receipts --------------------------
    "get_messages": _COMMS,
    "list_messages": _COMMS,
    "get_history": _COMMS,
    "get_message_context": _COMMS,
    "search_messages": _COMMS,
    "search_global": _COMMS,
    "get_pinned_messages": _COMMS,
    "get_scheduled_messages": _COMMS,
    "get_drafts": _COMMS,
    "get_message_reactions": _COMMS,
    "get_message_read_by": _COMMS,
    "list_inline_buttons": _COMMS,
    "get_media_info": _COMMS,
    "get_last_interaction": _COMMS,
    "get_recent_actions": _COMMS,
    # --- comms: chats, people, and lookups used to message them --------------
    "get_chats": _COMMS,
    "list_chats": _COMMS,
    "get_chat": _COMMS,
    "get_full_chat": _COMMS,
    "list_topics": _COMMS,
    "search_public_chats": _COMMS,
    "resolve_username": _COMMS,
    "get_common_chats": _COMMS,
    "get_message_link": _COMMS,
    "get_gif_search": _COMMS,
    "get_sticker_sets": _COMMS,
    "list_contacts": _COMMS,
    "search_contacts": _COMMS,
    "get_contact_ids": _COMMS,
    "get_direct_chat_by_contact": _COMMS,
    "get_contact_chats": _COMMS,
    "export_contacts": _COMMS,
    "get_blocked_users": _COMMS,
    "get_participants": _COMMS,
    "get_admins": _COMMS,
    "get_banned_users": _COMMS,
    "get_full_user": _COMMS,
    "get_user_photos": _COMMS,
    "get_user_status": _COMMS,
    "get_bot_info": _COMMS,
    # --- comms + files: media sent to or saved from people -------------------
    "send_file": _COMMS_FILES,
    "send_voice": _COMMS_FILES,
    "send_sticker": _COMMS_FILES,
    "upload_file": _COMMS_FILES,
    "download_media": _COMMS_FILES,
    # --- comms + control: membership, moderation and contact management ------
    "create_group": _COMMS_CONTROL,
    "create_channel": _COMMS_CONTROL,
    "invite_to_group": _COMMS_CONTROL,
    "leave_chat": _COMMS_CONTROL,
    "subscribe_public_channel": _COMMS_CONTROL,
    "join_chat_by_link": _COMMS_CONTROL,
    "import_chat_invite": _COMMS_CONTROL,
    "get_invite_link": _COMMS_CONTROL,
    "export_chat_invite": _COMMS_CONTROL,
    "edit_chat_title": _COMMS_CONTROL,
    "edit_chat_about": _COMMS_CONTROL,
    "delete_chat_photo": _COMMS_CONTROL,
    "promote_admin": _COMMS_CONTROL,
    "demote_admin": _COMMS_CONTROL,
    "edit_admin_rights": _COMMS_CONTROL,
    "ban_user": _COMMS_CONTROL,
    "unban_user": _COMMS_CONTROL,
    "set_default_chat_permissions": _COMMS_CONTROL,
    "toggle_slow_mode": _COMMS_CONTROL,
    "add_contact": _COMMS_CONTROL,
    "delete_contact": _COMMS_CONTROL,
    "import_contacts": _COMMS_CONTROL,
    "block_user": _COMMS_CONTROL,
    "unblock_user": _COMMS_CONTROL,
    # --- comms + control + files ---------------------------------------------
    "edit_chat_photo": ("comms", "control", "files"),
    # --- control: the account's own settings, folders and chat state ---------
    "mute_chat": _CONTROL,
    "unmute_chat": _CONTROL,
    "archive_chat": _CONTROL,
    "unarchive_chat": _CONTROL,
    "list_folders": _CONTROL,
    "get_folder": _CONTROL,
    "create_folder": _CONTROL,
    "add_chat_to_folder": _CONTROL,
    "remove_chat_from_folder": _CONTROL,
    "delete_folder": _CONTROL,
    "reorder_folders": _CONTROL,
    "get_privacy_settings": _CONTROL,
    "set_privacy_settings": _CONTROL,
    "list_accounts": _CONTROL,
    "get_me": _CONTROL,
    "set_bot_commands": _CONTROL,
    # --- body: the account's own profile -------------------------------------
    # A public bio is speech that reaches people, so update_profile is also comms.
    "update_profile": ("body", "comms"),
    "delete_profile_photo": ("body",),
    "set_profile_photo": ("body", "files"),
}

# Registered tools deliberately left without a class (most restrictive handling).
UNCLASSED: frozenset[str] = frozenset()


def apply_tool_classes(server: FastMCP) -> None:
    """Set ``_meta["mcpl/class"]`` on every registered tool named in ``TOOL_CLASSES``.

    Call once after all tool modules have registered. Tools not named stay
    unclassed. FastMCP (mcp >= 1.19) emits a tool's ``meta`` as ``_meta`` in
    tools/list but has no public setter for an already-registered tool, so this
    goes through the tool manager. Existing ``_meta`` keys are preserved.
    """
    for tool in server._tool_manager.list_tools():
        classes = TOOL_CLASSES.get(tool.name)
        if classes is not None:
            tool.meta = {**(tool.meta or {}), MCPL_CLASS_KEY: list(classes)}
