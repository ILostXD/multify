import os
import json
from typing import List, Dict, Any, Optional, Tuple
import tidalapi
from providers import BaseProvider

class TidalProvider(BaseProvider):
    name = "tidal"
    display_name = "Tidal"
    brand_color = "#000000"
    icon = "music"

    def _get_session(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> Optional[tidalapi.Session]:
        session = tidalapi.Session()
        
        # Check if saved in config or session
        tok_type = config.get("tidal_token_type") or session_data.get("tidal_token_type")
        access_tok = config.get("tidal_access_token") or session_data.get("tidal_access_token")
        refresh_tok = config.get("tidal_refresh_token") or session_data.get("tidal_refresh_token")
        expiry_time = config.get("tidal_expiry_time") or session_data.get("tidal_expiry_time")

        if tok_type and access_tok:
            try:
                session.load_oauth_session(tok_type, access_tok, refresh_tok, expiry_time)
                if session.check_login():
                    return session
            except Exception:
                pass

        # Check if tidal_session.json exists
        session_file = config.get("tidal_session_file", "tidal_session.json")
        if os.path.isfile(session_file):
            try:
                session.load_session_from_file(session_file)
                if session.check_login():
                    return session
            except Exception:
                pass

        return None

    def is_authenticated(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> bool:
        session = self._get_session(config, session_data)
        return session is not None and session.user is not None

    def search(self, artist: str, title: str, album: str, config: Dict[str, Any], session_data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        session = self._get_session(config, session_data)
        if not session:
            # Create a basic guest session if possible
            session = tidalapi.Session()

        query = f"{artist} {title}".strip() or title.strip()
        if not query:
            return [], None

        try:
            results = session.search(query, models=[tidalapi.media.Track], limit=8)
            tracks = results.get("tracks", []) if isinstance(results, dict) else (results.tracks if hasattr(results, "tracks") else [])
            
            out = []
            seen_ids = set()

            for t in tracks:
                tid = str(getattr(t, "id", ""))
                if not tid or tid in seen_ids:
                    continue
                seen_ids.add(tid)

                artist_name = getattr(t.artist, "name", "") if hasattr(t, "artist") and t.artist else ""
                album_obj = getattr(t, "album", None)
                album_name = getattr(album_obj, "name", "") if album_obj else ""
                
                # Get album cover image URL if available
                art = ""
                if album_obj and hasattr(album_obj, "image"):
                    try:
                        art = album_obj.image(320)
                    except Exception:
                        pass

                out.append({
                    "id": tid,
                    "uri": tid,
                    "name": getattr(t, "name", ""),
                    "artists": artist_name,
                    "album": album_name,
                    "album_art": art,
                    "duration_ms": getattr(t, "duration", 0) * 1000 if hasattr(t, "duration") else 0
                })

            return out[:8], None
        except Exception:
            return [], None

    def create_playlist(self, name: str, track_uris: List[str], config: Dict[str, Any], session_data: Dict[str, Any]) -> Dict[str, Any]:
        session = self._get_session(config, session_data)
        if not session or not session.user:
            return {
                "success": False,
                "error": "Tidal is not authenticated. Please log in to Tidal in Settings."
            }

        try:
            playlist = session.user.create_playlist(name, "Imported via M3UTify")
            if not playlist:
                return {"success": False, "error": "Failed to create playlist on Tidal."}

            playlist.add(track_uris)
            playlist_url = f"https://listen.tidal.com/playlist/{playlist.id}"

            return {
                "success": True,
                "playlist_name": name,
                "playlist_url": playlist_url,
                "track_count": len(track_uris)
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"Tidal Error: {str(e)}"
            }
