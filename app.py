#!/usr/bin/env python3
"""
Multify — Multi-Service Playlist Converter
-----------------------------------------
Self-hosted web app that converts .m3u8 playlists (e.g. exported from
Navidrome, Plex, Jellyfin) into Spotify, YouTube Music, or Tidal playlists,
with manual review of ambiguous matches and export of missing tracks.

Run with: python app.py
"""

import os
import re
import json
import time
import base64
import secrets
import urllib.parse
from pathlib import Path
from datetime import timedelta
from typing import Dict, Any

import requests
from flask import Flask, request, session, redirect, jsonify, render_template, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

from providers.registry import PROVIDERS, get_provider
import tidalapi

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

def _get_or_create_secret_key() -> str:
    """Ensure all Gunicorn workers and restarts share a persistent, stable secret key."""
    env_key = os.environ.get("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    try:
        cfg = load_config()
        if cfg.get("flask_secret_key"):
            return cfg["flask_secret_key"]
        new_key = secrets.token_hex(32)
        save_config({"flask_secret_key": new_key})
        return new_key
    except Exception:
        return "multify_persistent_fallback_secret_key_v1"

app.secret_key = _get_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

def get_config_file_path() -> str:
    """Resolve the actual config file path, safely handling Docker directory volume mounts."""
    target = os.environ.get("CONFIG_FILE") or os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.isdir(target):
        return os.path.join(target, "config.json")
    parent = os.path.dirname(target)
    if parent and not os.path.exists(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception:
            pass
    return target

CONFIG_FILE = get_config_file_path()

def load_config() -> Dict[str, Any]:
    """Load settings from config.json with fallback to environment variables."""
    cfg = {
        "spotify_client_id": os.environ.get("SPOTIFY_CLIENT_ID", ""),
        "spotify_client_secret": os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
        "spotify_redirect_uri": os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:5099/callback"),
        "ytmusic_headers": "",
        "ytmusic_oauth_path": "ytmusic_oauth.json",
        "tidal_token_type": "",
        "tidal_access_token": "",
        "tidal_refresh_token": "",
        "tidal_expiry_time": "",
    }
    cfg_file = get_config_file_path()
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for k, v in saved.items():
                    if v is not None:
                        cfg[k] = v
        except Exception as e:
            app.logger.warning(f"Failed to read config file ({cfg_file}): {e}")
    return cfg

def save_config(new_cfg: Dict[str, Any]) -> bool:
    """Save settings dictionary to config.json."""
    try:
        current = load_config()
        current.update(new_cfg)
        cfg_file = get_config_file_path()
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
        return True
    except Exception as e:
        app.logger.error(f"Failed to save config file ({get_config_file_path()}): {e}")
        return False

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
SCOPES = "playlist-modify-public playlist-modify-private playlist-read-private"


# ─────────────────────────────────────────────────────────────────────────
#  Assets & Web Routes
# ─────────────────────────────────────────────────────────────────────────

@app.route("/assets/<path:filename>")
def serve_assets(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "assets"), filename)


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "assets"),
        "Multify_BG_Color_Logo.svg",
        mimetype="image/svg+xml"
    )


@app.route("/")
def index():
    cfg = load_config()
    spotify_logged_in = bool(session.get("access_token"))
    return render_template("index.html", logged_in=spotify_logged_in)


@app.route("/api/providers/status", methods=["GET"])
def provider_status():
    cfg = load_config()
    sess_dict = dict(session)
    statuses = {}
    for name, prov in PROVIDERS.items():
        statuses[name] = {
            "name": prov.name,
            "display_name": prov.display_name,
            "brand_color": prov.brand_color,
            "icon": prov.icon,
            "is_authenticated": prov.is_authenticated(cfg, sess_dict),
        }
    return jsonify({"providers": statuses})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    cfg = load_config()
    return jsonify({
        "spotify_client_id": cfg.get("spotify_client_id", ""),
        "spotify_client_secret": cfg.get("spotify_client_secret", ""),
        "spotify_redirect_uri": cfg.get("spotify_redirect_uri", "http://127.0.0.1:5099/callback"),
        "ytmusic_headers": cfg.get("ytmusic_headers", ""),
        "has_tidal": bool(cfg.get("tidal_access_token")),
        "is_configured": bool(cfg.get("spotify_client_id") and cfg.get("spotify_client_secret")),
    })


@app.route("/api/settings", methods=["POST"])
def update_settings():
    data = request.get_json() or {}
    new_cfg = {}
    
    if "spotify_client_id" in data:
        new_cfg["spotify_client_id"] = str(data["spotify_client_id"]).strip()
    if "spotify_client_secret" in data:
        new_cfg["spotify_client_secret"] = str(data["spotify_client_secret"]).strip()
    if "spotify_redirect_uri" in data:
        new_cfg["spotify_redirect_uri"] = str(data["spotify_redirect_uri"]).strip()
    if "ytmusic_headers" in data:
        new_cfg["ytmusic_headers"] = str(data["ytmusic_headers"]).strip()

    if save_config(new_cfg):
        return jsonify({"success": True, "message": "Settings saved successfully."})
    return jsonify({"error": "Failed to write config file"}), 500


# ─────────────────────────────────────────────────────────────────────────
#  Spotify OAuth
# ─────────────────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    session.permanent = True
    cfg = load_config()
    client_id = cfg.get("spotify_client_id")
    redirect_uri = cfg.get("spotify_redirect_uri", "http://127.0.0.1:5099/callback")

    if not client_id:
        return "Spotify Client ID not configured. Please open Settings in the web UI.", 400

    state = secrets.token_hex(16)
    session["oauth_state"] = state
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "show_dialog": "true",
    }
    return redirect(f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/callback")
def callback():
    if request.args.get("state") != session.get("oauth_state"):
        return "State mismatch — please try logging in again.", 400
    code = request.args.get("code")
    if not code:
        return f"Spotify auth failed: {request.args.get('error', 'unknown error')}", 400

    cfg = load_config()
    client_id = cfg.get("spotify_client_id")
    client_secret = cfg.get("spotify_client_secret")
    redirect_uri = cfg.get("spotify_redirect_uri", "http://127.0.0.1:5099/callback")

    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        SPOTIFY_TOKEN_URL,
        headers={"Authorization": f"Basic {auth_header}"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        timeout=15
    )
    if resp.status_code != 200:
        return f"Token exchange failed: {resp.text}", 400

    data = resp.json()
    session.permanent = True
    session["access_token"] = data["access_token"]
    session["refresh_token"] = data.get("refresh_token")
    session["token_expires_at"] = time.time() + data.get("expires_in", 3600) - 120
    save_config({"spotify_access_token": data["access_token"], "spotify_refresh_token": data.get("refresh_token") or cfg.get("spotify_refresh_token", ""), "spotify_token_expires_at": session["token_expires_at"]})
    return redirect("/")


def _refresh_spotify_token_if_needed():
    """Auto-refresh the Spotify access token if it's expired."""
    cfg = load_config()
    expires_at = session.get("token_expires_at") or cfg.get("spotify_token_expires_at", 0)
    refresh_tok = session.get("refresh_token") or cfg.get("spotify_refresh_token")

    if not session.get("access_token") and cfg.get("spotify_access_token") and time.time() < expires_at:
        session["access_token"] = cfg.get("spotify_access_token")
        session["refresh_token"] = refresh_tok
        session["token_expires_at"] = expires_at
        return

    if not refresh_tok or time.time() < expires_at:
        return

    cfg = load_config()
    client_id = cfg.get("spotify_client_id")
    client_secret = cfg.get("spotify_client_secret")
    if not client_id or not client_secret:
        return

    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            SPOTIFY_TOKEN_URL,
            headers={"Authorization": f"Basic {auth_header}"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
            },
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            session["access_token"] = data["access_token"]
            if data.get("refresh_token"):
                session["refresh_token"] = data["refresh_token"]
            session["token_expires_at"] = time.time() + data.get("expires_in", 3600) - 120
            save_config({
                "spotify_access_token": session["access_token"],
                "spotify_refresh_token": session.get("refresh_token") or cfg.get("spotify_refresh_token", ""),
                "spotify_token_expires_at": session["token_expires_at"]
            })
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────
#  Tidal OAuth Device Flow
# ─────────────────────────────────────────────────────────────────────────

TIDAL_ACTIVE_SESSIONS: Dict[str, Any] = {}

@app.route("/api/tidal/login", methods=["POST"])
def tidal_start_login():
    """Initiates Tidal OAuth Device flow and returns link URL."""
    try:
        t_session = tidalapi.Session()
        login_key, uri, expires = t_session.login_oauth()
        session_id = secrets.token_hex(12)
        TIDAL_ACTIVE_SESSIONS[session_id] = {
            "session": t_session,
            "login_key": login_key,
            "created_at": time.time()
        }
        return jsonify({
            "session_id": session_id,
            "login_url": uri,
            "expires_in": expires
        })
    except Exception as e:
        return jsonify({"error": f"Failed to start Tidal login: {str(e)}"}), 500


@app.route("/api/tidal/check_login", methods=["POST"])
def tidal_check_login():
    """Checks if the user completed the Tidal authorization."""
    data = request.get_json() or {}
    session_id = data.get("session_id")
    sess_obj = TIDAL_ACTIVE_SESSIONS.get(session_id)
    if not sess_obj:
        return jsonify({"error": "Login session expired or not found."}), 400

    t_session: tidalapi.Session = sess_obj["session"]
    login_key = sess_obj["login_key"]

    try:
        is_logged_in = t_session.process_link_login(login_key)
        if is_logged_in and t_session.check_login():
            save_config({
                "tidal_token_type": t_session.token_type,
                "tidal_access_token": t_session.access_token,
                "tidal_refresh_token": t_session.refresh_token,
                "tidal_expiry_time": str(t_session.expiry_time),
            })
            del TIDAL_ACTIVE_SESSIONS[session_id]
            return jsonify({"success": True, "message": "Successfully connected Tidal!"})
        else:
            return jsonify({"pending": True, "message": "Waiting for authorization on Tidal..."})
    except Exception as e:
        return jsonify({"error": f"Tidal check failed: {str(e)}"}), 400


# ─────────────────────────────────────────────────────────────────────────
#  M3U8 Parsing
# ─────────────────────────────────────────────────────────────────────────

def parse_m3u8(content: str):
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    tracks = []
    pending_extinf = None

    for line in lines:
        if line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:(-?\d+),(.*)", line)
            if m:
                info = m.group(2)
                if " - " in info:
                    parts = info.split(" - ", 1)
                    artist, title = parts[0], parts[1]
                else:
                    artist, title = "", info
                pending_extinf = {"artist": artist.strip(), "title": title.strip()}
            continue
        if line.startswith("#"):
            continue

        path = line
        entry = {
            "path": path,
            "artist": pending_extinf["artist"] if pending_extinf else "",
            "title": pending_extinf["title"] if pending_extinf else "",
        }
        pending_extinf = None
        tracks.append(entry)

    return tracks


def extract_metadata_from_path(path: str):
    if not path:
        return {"artist": "", "title": "", "album": ""}

    if os.path.isfile(path) and MutagenFile:
        try:
            f = MutagenFile(path, easy=True)
            if f:
                artist = (f.get("artist") or [""])[0]
                title = (f.get("title") or [""])[0]
                album = (f.get("album") or [""])[0]
                if artist or title:
                    return {"artist": artist, "title": title, "album": album}
        except Exception:
            pass

    normalized = path.replace("\\", "/").strip()
    parts = [p for p in normalized.split("/") if p]
    filename = parts[-1] if parts else normalized
    stem = Path(filename).stem
    cleaned_stem = re.sub(r"^\d+[\.\-\s_]+", "", stem).strip()

    artist, title, album = "", "", ""
    if " - " in cleaned_stem:
        parts_dash = cleaned_stem.split(" - ")
        if len(parts_dash) >= 2:
            artist = parts_dash[0].strip()
            title = " - ".join(parts_dash[1:]).strip()
    else:
        title = cleaned_stem

    if not artist and len(parts) >= 3:
        possible_album = parts[-2].strip()
        possible_artist = parts[-3].strip()
        if possible_artist and possible_artist.lower() not in ("music", "songs", "tracks", "media", "audio", "playlists"):
            artist = possible_artist
            album = possible_album
    elif not artist and len(parts) >= 2:
        possible_artist = parts[-2].strip()
        if possible_artist and possible_artist.lower() not in ("music", "songs", "tracks", "media", "audio", "playlists"):
            artist = possible_artist

    return {"artist": artist, "title": title or stem, "album": album}


@app.route("/parse", methods=["POST"])
def parse_playlist():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    content = request.files["file"].read().decode("utf-8", errors="ignore")
    raw_tracks = parse_m3u8(content)

    enriched = []
    for t in raw_tracks:
        artist = t.get("artist", "").strip()
        title = t.get("title", "").strip()
        album = ""

        if not artist or not title:
            meta = extract_metadata_from_path(t.get("path", ""))
            if not artist:
                artist = meta["artist"]
            if not title:
                title = meta["title"]
            if not album:
                album = meta["album"]

        enriched.append({
            "path": t.get("path", ""),
            "artist": artist,
            "title": title,
            "album": album,
        })

    return jsonify({"tracks": enriched})


# ─────────────────────────────────────────────────────────────────────────
#  Unified Multi-Service Search & Creation
# ─────────────────────────────────────────────────────────────────────────

SEARCH_CACHE: Dict[str, Any] = {}

@app.route("/search", methods=["POST"])
def search_track():
    _refresh_spotify_token_if_needed()
    data = request.json or {}
    provider_name = data.get("provider", "spotify").lower()
    artist = (data.get("artist") or "").strip()
    title = (data.get("title") or "").strip()
    album = (data.get("album") or "").strip()

    if not title:
        return jsonify({"results": []})

    cfg = load_config()
    sess_dict = dict(session)
    prov = get_provider(provider_name)

    if provider_name == "spotify" and not prov.is_authenticated(cfg, sess_dict):
        return jsonify({"error": "Not authenticated with Spotify"}), 401

    cache_key = f"{provider_name}:{artist.lower()}:{title.lower()}:{album.lower()}"
    if cache_key in SEARCH_CACHE:
        return jsonify({"results": SEARCH_CACHE[cache_key]})

    results, retry_after = prov.search(artist, title, album, cfg, sess_dict)
    if retry_after:
        return jsonify({
            "error": "rate_limited",
            "message": f"Rate limit reached on {prov.display_name}.",
            "retry_after": retry_after
        }), 429

    SEARCH_CACHE[cache_key] = results
    return jsonify({"results": results})


@app.route("/create_playlist", methods=["POST"])
def create_playlist_route():
    _refresh_spotify_token_if_needed()
    data = request.json or {}
    provider_name = data.get("provider", "spotify").lower()
    name = data.get("name", "Imported Playlist")
    uris = data.get("uris", [])

    if not uris:
        return jsonify({"error": "No tracks selected"}), 400

    cfg = load_config()
    sess_dict = dict(session)
    prov = get_provider(provider_name)

    res = prov.create_playlist(name, uris, cfg, sess_dict)
    if not res.get("success"):
        return jsonify({"error": res.get("error", "Playlist creation failed.")}), 400

    return jsonify(res)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5099, debug=False)
