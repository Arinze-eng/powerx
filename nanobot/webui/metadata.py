"""Shared WebUI metadata keys."""

WEBUI_TURN_METADATA_KEY = "webui_turn_id"
WEBUI_SYSTEM_COMMAND_TURN_PREFIX = "webui-system:"
WEBSOCKET_TURN_OWNER_METADATA_KEY = "_websocket_turn_owner"
WEBUI_MESSAGE_SOURCE_METADATA_KEY = "_webui_message_source"
# [FIX 2026-09-04] Owner (Supabase user id) that created a WebUI chat session.
# Used to isolate chat history per user so users cannot see each other's chats.
WEBUI_SESSION_OWNER_KEY = "_webui_owner_user_id"
