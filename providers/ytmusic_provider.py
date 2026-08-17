import json
import os
from typing import List, Dict, Any, Optional, Tuple
from ytmusicapi import YTMusic
from providers import BaseProvider

class YouTubeMusicProvider(BaseProvider):
    name = "ytmusic"
    display_name = "YouTube Music"
    brand_color = "#ff0000"
    icon = "play-circle"

    def _get_client(self, config: Dict[str, Any], authenticated_only: bool = False) -> Optional[YTMusic]:
        # 1. Check if oauth.json exists on disk
        oauth_path = config.get("ytmusic_oauth_path", "ytmusic_oauth.json")
        if os.path.isfile(oauth_path):
            try:
                return YTMusic(oauth_path)
            except Exception:
                pass

        # 2. Check if raw browser headers are provided in config
        headers_raw = config.get("ytmusic_headers", "").strip()
        if headers_raw:
            try:
                # If valid JSON string
                if headers_raw.startswith("{"):
                    return YTMusic(headers_raw)
                # If raw multi-line request headers text
                else:
                    return YTMusic(headers_raw)
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
                "error": "YouTube Music is not authenticated. Please provide your OAuth tokens or browser headers in Settings."
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
                "track_count": len(track_uris)
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"YouTube Music Error: {str(e)}"
            }
