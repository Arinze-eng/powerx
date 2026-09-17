"""YouTube (Google) integration for nanobot.

Provides per-user Google OAuth token storage plus a small YouTube Data API v3
client used by the ``youtube`` agent tool and the WebUI settings connector.
"""

from __future__ import annotations

from nanobot.youtube.credentials import (
    YouTubeCredentialError,
    YouTubeCredentialStore,
)
from nanobot.youtube.oauth import (
    YouTubeOAuthError,
    YouTubeOAuthManager,
    get_youtube_oauth_manager,
)

__all__ = [
    "YouTubeCredentialError",
    "YouTubeCredentialStore",
    "YouTubeOAuthError",
    "YouTubeOAuthManager",
    "get_youtube_oauth_manager",
]
