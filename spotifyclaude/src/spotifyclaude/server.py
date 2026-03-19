# ─────────────────────────────────────────────────────────────────────────────
# Spotify MCP Server
# Built for the Claude Builders Club @ UCC
#
# This file implements a Model Context Protocol (MCP) server that exposes
# Spotify actions as tools that Claude can call. MCP is Anthropic's open
# standard for connecting AI models to external services.
#
# How it works:
#   1. Claude connects to this server over stdio.
#   2. Claude discovers available tools via the list_tools handler.
#   3. When Claude calls a tool, handle_call_tool executes the matching logic,
#      calls the Spotify Web API, and returns a plain-text result.
#
# Setup:
#   - Create a Spotify app at https://developer.spotify.com/dashboard
#   - Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET as environment variables
#   - Add http://localhost:8000/callback as a Redirect URI in your app settings
#   - Run `authorize` first to complete the OAuth flow and cache a token
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import json
import os
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import httpx
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

# ── Spotify credentials ──────────────────────────────────────────────
# Set these as environment variables or replace the defaults below.
# Never hard-code real credentials — keep them out of version control.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "YOUR_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "YOUR_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://localhost:8000/callback")

# OAuth scopes determine what the app is allowed to do on behalf of the user.
# We request read access for playback state and write access for playlists.
SCOPES = "user-read-currently-playing user-read-playback-state user-read-private playlist-modify-public playlist-modify-private"

# Token is cached on disk so the user only needs to authorize once.
TOKEN_PATH = Path.home() / ".spotify_mcp_token.json"

# The MCP server instance — all tool definitions and handlers attach to this.
server = Server("spotifyclaude")

# ── Token management ─────────────────────────────────────────────────

def load_token() -> dict | None:
    """Load the cached OAuth token from disk.

    Returns the token dict if it exists and is still valid, attempts a refresh
    if it has expired, and returns None if no usable token is found.
    """
    if TOKEN_PATH.exists():
        data = json.loads(TOKEN_PATH.read_text())
        # Check expiry timestamp we wrote at save time
        if data.get("expires_at", 0) > time.time():
            return data
        # Token expired — try to get a new one using the refresh token
        if data.get("refresh_token"):
            refreshed = _refresh_token(data["refresh_token"])
            if refreshed:
                return refreshed
    return None


def save_token(data: dict):
    """Persist the token to disk, adding an absolute expiry timestamp."""
    # expires_in is a relative number of seconds from Spotify; convert to epoch.
    # Subtract 60 s to refresh slightly before the real expiry.
    data["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    TOKEN_PATH.write_text(json.dumps(data))


def _refresh_token(refresh_token: str) -> dict | None:
    """Exchange a refresh token for a new access token without user interaction.

    Spotify issues a new access token (and sometimes a new refresh token).
    Returns the token dict on success, or None if the refresh fails.
    """
    resp = httpx.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
    )
    if resp.status_code == 200:
        data = resp.json()
        # Spotify may not return a new refresh token; keep the old one if so
        data.setdefault("refresh_token", refresh_token)
        save_token(data)
        return data
    return None


def get_access_token() -> str:
    """Return a valid access token, raising if the user hasn't authorized yet."""
    token = load_token()
    if token:
        return token["access_token"]
    raise RuntimeError(
        "Not authenticated. Use the 'authorize' tool first to connect your Spotify account."
    )


def _do_oauth_flow() -> dict:
    """Run a one-shot local HTTP server to capture the OAuth callback.

    Steps:
      1. Build the Spotify authorization URL with the required scopes.
      2. Open it in the user's default browser.
      3. Spin up a temporary HTTP server on localhost to catch the redirect.
      4. Exchange the authorization code for an access + refresh token pair.
      5. Save the token to disk and return it.
    """
    auth_url = (
        "https://accounts.spotify.com/authorize?"
        + urllib.parse.urlencode(
            {
                "client_id": SPOTIFY_CLIENT_ID,
                "response_type": "code",   # Authorization Code flow
                "redirect_uri": SPOTIFY_REDIRECT_URI,
                "scope": SCOPES,
            }
        )
    )

    auth_code = None

    class Handler(BaseHTTPRequestHandler):
        """Minimal HTTP handler that captures the ?code= query parameter."""
        def do_GET(self):
            nonlocal auth_code
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            auth_code = params.get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Success! You can close this tab.</h1>")

        def log_message(self, format, *args):
            pass  # suppress noisy access logs

    parsed = urllib.parse.urlparse(SPOTIFY_REDIRECT_URI)
    port = parsed.port or 8000

    # Start the server, open the browser, then block until one request arrives
    srv = HTTPServer(("localhost", port), Handler)
    webbrowser.open(auth_url)
    srv.handle_request()  # handle one request then stop
    srv.server_close()

    if not auth_code:
        raise RuntimeError("OAuth failed — no authorization code received.")

    # Exchange the authorization code for access and refresh tokens
    resp = httpx.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": auth_code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    save_token(data)
    return data


# ── Spotify API helpers ──────────────────────────────────────────────

def spotify_get(endpoint: str, params: dict | None = None) -> dict:
    """Make an authenticated GET request to the Spotify Web API.

    Args:
        endpoint: Path relative to https://api.spotify.com/v1/ (e.g. "me").
        params:   Optional query parameters dict.

    Returns the parsed JSON response, or raises on HTTP errors.
    """
    token = get_access_token()
    resp = httpx.get(
        f"https://api.spotify.com/v1/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    resp.raise_for_status()
    return resp.json()


def spotify_post(endpoint: str, json_body: dict | None = None) -> dict:
    """Make an authenticated POST request to the Spotify Web API.

    Args:
        endpoint:  Path relative to https://api.spotify.com/v1/.
        json_body: Request body to send as JSON.

    Returns the parsed JSON response, or raises on HTTP errors.
    """
    token = get_access_token()
    resp = httpx.post(
        f"https://api.spotify.com/v1/{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        json=json_body,
    )
    resp.raise_for_status()
    return resp.json()


# ── MCP Tools ────────────────────────────────────────────────────────
# Each tool below is surfaced to Claude via the MCP protocol.
# Claude reads the name and description to decide when to call each tool,
# and uses the inputSchema to know what arguments to pass.

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """Advertise all available tools to the MCP client (Claude)."""
    return [
        # ── authorize ────────────────────────────────────────────────
        # Must be called once before any other tool. Opens a browser window
        # for the user to log in with their Spotify account.
        types.Tool(
            name="authorize",
            description="Connect your Spotify account. Opens a browser for login. Run this first!",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── get_current_song ─────────────────────────────────────────
        # Reads the currently active playback state from Spotify.
        types.Tool(
            name="get_current_song",
            description="Get the currently playing song on Spotify",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── search_track ─────────────────────────────────────────────
        # Searches the Spotify catalogue and returns the top 5 matches
        # with their track URIs (needed to add tracks to playlists).
        types.Tool(
            name="search_track",
            description="Search for a track on Spotify",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (song name, artist, etc.)",
                    }
                },
                "required": ["query"],
            },
        ),

        # ── create_playlist ──────────────────────────────────────────
        # Creates a new private playlist on the authenticated user's account.
        # Uses POST /me/playlists (not /users/{id}/playlists) to avoid
        # 403 errors introduced by Spotify's February 2026 API changes.
        types.Tool(
            name="create_playlist",
            description="Create a new playlist on your Spotify account",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for the new playlist",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description for the playlist",
                    },
                },
                "required": ["name"],
            },
        ),

        # ── add_tracks ───────────────────────────────────────────────
        # Adds one or more tracks to an existing playlist by URI.
        # Uses the /items endpoint (renamed from /tracks in February 2026).
        types.Tool(
            name="add_tracks",
            description="Add tracks to a Spotify playlist",
            inputSchema={
                "type": "object",
                "properties": {
                    "playlist_id": {
                        "type": "string",
                        "description": "The Spotify playlist ID or URI",
                    },
                    "uris": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of Spotify track URIs (e.g. spotify:track:4iV5W9uYEdYUVa79Axb7Rh)",
                    },
                },
                "required": ["playlist_id", "uris"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent]:
    """Dispatch a tool call from Claude to the appropriate handler.

    All Spotify API errors are caught and returned as readable text so Claude
    can explain the problem to the user rather than crashing.
    """
    arguments = arguments or {}

    try:
        if name == "authorize":
            # Run the browser-based OAuth flow and cache the token
            _do_oauth_flow()
            return [types.TextContent(type="text", text="Successfully connected to Spotify!")]

        elif name == "get_current_song":
            data = spotify_get("me/player/currently-playing")
            if not data or not data.get("item"):
                return [types.TextContent(type="text", text="Nothing is currently playing.")]
            track = data["item"]
            artists = ", ".join(a["name"] for a in track["artists"])
            return [
                types.TextContent(
                    type="text",
                    text=f"Now playing: {track['name']} by {artists}\n"
                    f"Album: {track['album']['name']}\n"
                    f"URL: {track['external_urls'].get('spotify', 'N/A')}",
                )
            ]

        elif name == "search_track":
            query = arguments.get("query")
            if not query:
                raise ValueError("Missing 'query' argument")
            # Search the Spotify catalogue; limit to 5 results for readability
            data = spotify_get("search", {"q": query, "type": "track", "limit": 5})
            tracks = data.get("tracks", {}).get("items", [])
            if not tracks:
                return [types.TextContent(type="text", text=f"No results for '{query}'.")]
            lines = []
            for i, t in enumerate(tracks, 1):
                artists = ", ".join(a["name"] for a in t["artists"])
                lines.append(f"{i}. {t['name']} — {artists} ({t['album']['name']})")
                # Include the URI so Claude can pass it straight to add_tracks
                lines.append(f"   URI: {t['uri']}")
            return [types.TextContent(type="text", text="\n".join(lines))]

        elif name == "create_playlist":
            playlist_name = arguments.get("name")
            if not playlist_name:
                raise ValueError("Missing 'name' argument")
            description = arguments.get("description", "")
            # POST to /me/playlists — avoids 403s from the Feb 2026 API changes
            playlist = spotify_post(
                "me/playlists",
                {"name": playlist_name, "description": description, "public": False},
            )
            return [
                types.TextContent(
                    type="text",
                    text=f"Created playlist '{playlist_name}'!\nURL: {playlist['external_urls'].get('spotify', 'N/A')}",
                )
            ]

        elif name == "add_tracks":
            playlist_id = arguments.get("playlist_id", "")
            uris = arguments.get("uris", [])
            if not playlist_id:
                raise ValueError("Missing 'playlist_id' argument")
            if not uris:
                raise ValueError("Missing 'uris' argument")
            # Accept either a bare ID or a full spotify:playlist:... URI
            playlist_id = playlist_id.removeprefix("spotify:playlist:")
            # /items is the correct endpoint as of the February 2026 API update
            # (previously named /tracks)
            spotify_post(
                f"playlists/{playlist_id}/items",
                {"uris": uris},
            )
            return [
                types.TextContent(
                    type="text",
                    text=f"Added {len(uris)} track(s) to playlist {playlist_id}.",
                )
            ]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except RuntimeError as e:
        return [types.TextContent(type="text", text=str(e))]
    except httpx.HTTPStatusError as e:
        return [types.TextContent(type="text", text=f"Spotify API error: {e.response.status_code} — {e.response.text}")]


async def main():
    """Entry point — connect the server to Claude via stdio."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="spotifyclaude",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
