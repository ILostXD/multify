import json
import os
from typing import List, Dict, Any, Optional, Tuple
from ytmusicapi import YTMusic
from ytmusicapi.setup import setup_browser
from ytmusicapi.auth.oauth import OAuthCredentials
from providers import BaseProvider

class YouTubeMusicProvider(BaseProvider):
    name = "ytmusic"
    display_name = "YouTube Music"
    brand_color = "#ff0000"
    icon = "play-circle"

    def _get_client(self, config: Dict[str, Any], authenticated_only: bool = False) -> Optional[YTMusic]:
        # 1. Check if Google OAuth token JSON exists on disk
        oauth_path = config.get("ytmusic_oauth_path") or os.environ.get("YTMUSIC_OAUTH_PATH", "ytmusic_oauth.json")
        client_id = config.get("ytmusic_client_id") or os.environ.get("MULTIFY_YTMUSIC_CLIENT_ID") or os.environ.get("YTMUSIC_CLIENT_ID")
        client_secret = config.get("ytmusic_client_secret") or os.environ.get("MULTIFY_YTMUSIC_CLIENT_SECRET") or os.environ.get("YTMUSIC_CLIENT_SECRET")

        oauth_creds = None
        if client_id and client_secret:
            try:
                oauth_creds = OAuthCredentials(client_id, client_secret)
            except Exception:
                pass

        if os.path.isfile(oauth_path):
            try:
                client = YTMusic(oauth_path, oauth_credentials=oauth_creds)
                if client:
                    return client
            except Exception:
                pass

        # 2. Check if browser headers are provided in config
        headers_raw = (config.get("ytmusic_headers") or "").strip()
        if headers_raw:
            try:
                # If already a valid json string of headers
                if headers_raw.startswith("{") and "cookie" in headers_raw.lower():
                    client = YTMusic(auth=headers_raw)
                    return client
                else:
                    # Convert raw request headers text using ytmusicapi setup_browser
                    parsed_json = setup_browser(headers_raw=headers_raw)
                    client = YTMusic(auth=parsed_json)
                    return client
            except Exception:
                try:
                    return YTMusic(auth=headers_raw)
                except Exception:
                    pass

        if authenticated_only:
            return None

        # 3. Fallback to public unauthenticated client (can search full catalog!)
        try:
            return YTMusic()
        except Exception:
            return None

    def is_authenticated(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> bool:
        client = self._get_client(config, authenticated_only=True)
        return client is not None

    def search(self, artist: str, title: str, album: str, config: Dict[str, Any], session_data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        client = self._get_client(config, authenticated_only=False)
        if not client:
            return [], None

        query = f"{artist} {title}".strip() or title.strip()
        if not query:
            return [], None

        try:
            raw_results = client.search(query, filter="songs", limit=8)
            out = []
            seen_ids = set()

            for item in raw_results:
                vid = item.get("videoId")
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)

                artists_list = item.get("artists", [])
                artist_names = ", ".join(a.get("name", "") for a in artists_list if a.get("name"))
                album_info = item.get("album") or {}
                album_name = album_info.get("name", "") if isinstance(album_info, dict) else ""
                
                thumbnails = item.get("thumbnails", [])
                art = thumbnails[-1]["url"] if thumbnails else ""
                if art and art.startswith("//"):
                    art = "https:" + art

                out.append({
                    "id": vid,
                    "uri": vid, # videoId used for playlist addition
                    "name": item.get("title", ""),
                    "artists": artist_names,
                    "album": album_name,
                    "album_art": art,
                    "duration": item.get("duration", ""),
                    "duration_seconds": item.get("duration_seconds", 0)
                })

            return out[:8], None
        except Exception:
            return [], None

    def create_playlist(self, name: str, track_uris: List[str], config: Dict[str, Any], session_data: Dict[str, Any]) -> Dict[str, Any]:
        client = self._get_client(config, authenticated_only=True)
        if not client:
            return {
                "success": False,
                "error": "YouTube Music is not authenticated. Please paste your browser headers in Settings."
            }

        try:
            # video_ids are the track_uris
            playlist_id = client.create_playlist(
                title=name,
                description="Imported via Multify",
                privacy_status="PRIVATE",
                video_ids=track_uris
            )

            playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"

            return {
                "success": True,
                "playlist_name": name,
                "playlist_url": playlist_url,
                "playlist_id": playlist_id,
                "added_count": len(track_uris)
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"YouTube Music Error: {str(e)}"
            }
