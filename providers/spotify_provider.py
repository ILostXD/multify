import re
import time
import requests
from typing import List, Dict, Any, Optional, Tuple
from providers import BaseProvider

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

class SpotifyProvider(BaseProvider):
    name = "spotify"
    display_name = "Spotify"
    brand_color = "#1db954"
    icon = "disc"

    def is_authenticated(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> bool:
        return bool(session_data.get("access_token"))

    def get_auth_header(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
        token = session_data.get("access_token")
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    def search(self, artist: str, title: str, album: str, config: Dict[str, Any], session_data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        headers = self.get_auth_header(config, session_data)
        if not headers:
            return [], None

        results = []
        seen_ids = set()

        clean_title = re.sub(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*", " ", title).strip() or title
        clean_artist = re.sub(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*", " ", artist).strip() or artist
        clean_album = re.sub(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*", " ", album).strip() if album else ""

        # Strategy 1: Plain relevance query (invokes Spotify's official popularity algorithm)
        if clean_artist and clean_title:
            q1 = f"{clean_artist} {clean_title}".strip()
            hits, retry = self._spotify_search(q1, headers, seen_ids)
            if retry:
                return [], retry
            results += hits

        # Strategy 2: If album is known from file tags
        if clean_artist and clean_album and clean_title:
            q2 = f"{clean_artist} {clean_album} {clean_title}".strip()
            hits, retry = self._spotify_search(q2, headers, seen_ids)
            if retry:
                return [], retry
            results += hits

        # Strategy 3: Strict track & artist search
        if clean_artist and clean_title:
            q3 = f'track:"{clean_title}" artist:"{clean_artist}"'
            hits, retry = self._spotify_search(q3, headers, seen_ids)
            if retry:
                return [], retry
            results += hits
        elif clean_title:
            hits, retry = self._spotify_search(title.strip(), headers, seen_ids)
            if retry:
                return [], retry
            results += hits

        if not results:
            return results, None

        # Prioritize studio albums over random playlists/compilations
        artist_lower = artist.lower().strip()
        album_lower = album.lower().strip() if album else ""
        compilation_keywords = [
            "greatest", "50 jahre", "hip hop", "hits", "best of", "top ", "playlist",
            "vol.", "anthology", "collection", "tribute", "classics", "summer",
            "workout", "party", "favourites", "capsule", "essence", "just rap",
            "chill", "rhythmic", "fyp", "virales", "top songs", "new years", "compilation"
        ]

        def _sort_key(item):
            track_artists = item.get("artists", "").lower()
            album_artists = item.get("album_artists", "").lower()
            item_album_name = item.get("album", "").lower()
            album_type = item.get("album_type", "compilation")

            is_artist_album = bool(artist_lower and artist_lower in album_artists)
            is_track_artist = bool(artist_lower and artist_lower in track_artists)
            is_various = "various" in album_artists
            is_compilation_named = any(kw in item_album_name for kw in compilation_keywords)
            is_compilation = is_various or album_type == "compilation" or is_compilation_named
            exact_album_match = bool(album_lower and (album_lower in item_album_name or item_album_name in album_lower))

            if exact_album_match and is_artist_album:
                tier = -1
            elif is_artist_album and not is_compilation and album_type == "album":
                tier = 0
            elif is_artist_album and not is_compilation and album_type == "single":
                tier = 1
            elif is_artist_album:
                tier = 2
            elif is_track_artist and is_compilation:
                tier = 10
            elif is_track_artist:
                tier = 15
            else:
                tier = 20

            popularity = item.get("popularity", 0)
            return (tier, -popularity)

        results.sort(key=_sort_key)
        return results[:8], None

    def _spotify_search(self, query: str, headers: Dict[str, str], seen_ids: set) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        url = f"{SPOTIFY_API_BASE}/search"
        params = {"q": query, "type": "track", "limit": 6}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 429:
                hdr = resp.headers.get("Retry-After", "5")
                try:
                    return [], int(hdr)
                except ValueError:
                    return [], 5
            if resp.status_code != 200:
                return [], None

            data = resp.json()
            tracks = data.get("tracks", {}).get("items", [])
            out = []
            for t in tracks:
                tid = t.get("id")
                if not tid or tid in seen_ids:
                    continue
                seen_ids.add(tid)
                album_info = t.get("album", {})
                images = album_info.get("images", [])
                art = images[0]["url"] if images else ""
                album_artists_str = ", ".join(a["name"] for a in album_info.get("artists", []))
                out.append({
                    "id": tid,
                    "uri": t["uri"],
                    "name": t["name"],
                    "artists": ", ".join(a["name"] for a in t.get("artists", [])),
                    "album": album_info.get("name", ""),
                    "album_art": art,
                    "album_type": album_info.get("album_type", "album"),
                    "album_artists": album_artists_str,
                    "popularity": t.get("popularity", 0),
                    "duration_ms": t.get("duration_ms", 0),
                    "preview_url": t.get("preview_url")
                })
            return out, None
        except Exception:
            return [], None

    def create_playlist(self, name: str, track_uris: List[str], config: Dict[str, Any], session_data: Dict[str, Any]) -> Dict[str, Any]:
        headers = self.get_auth_header(config, session_data)
        if not headers:
            return {"success": False, "error": "Not authenticated with Spotify."}

        # Filter and validate Spotify track URIs
        valid_uris = [u for u in track_uris if u and (u.startswith("spotify:track:") or u.startswith("spotify:episode:"))]
        if not valid_uris:
            return {"success": False, "error": "No valid Spotify track matches found to add to playlist."}

        # 1. Create Playlist using /me/playlists (modern endpoint) with fallback to /users/{id}/playlists
        create_resp = requests.post(
            f"{SPOTIFY_API_BASE}/me/playlists",
            headers={**headers, "Content-Type": "application/json"},
            json={"name": name, "public": False, "description": "Imported via Mutify"},
            timeout=10
        )

        if create_resp.status_code not in (200, 201):
            # Fallback to /users/{user_id}/playlists
            me_resp = requests.get(f"{SPOTIFY_API_BASE}/me", headers=headers, timeout=10)
            if me_resp.status_code == 200:
                user_id = me_resp.json().get("id")
                create_resp = requests.post(
                    f"{SPOTIFY_API_BASE}/users/{user_id}/playlists",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"name": name, "public": False, "description": "Imported via Mutify"},
                    timeout=10
                )

        if create_resp.status_code not in (200, 201):
            return {"success": False, "error": f"Failed to create playlist: {create_resp.text}"}

        pdata = create_resp.json()
        playlist_id = pdata.get("id")
        playlist_url = pdata.get("external_urls", {}).get("spotify", "")

        # 2. Add tracks in batches of 100
        total_added = 0
        batch_size = 100
        for i in range(0, len(valid_uris), batch_size):
            batch = valid_uris[i:i + batch_size]
            add_resp = requests.post(
                f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks",
                headers={**headers, "Content-Type": "application/json"},
                json={"uris": batch},
                timeout=15
            )
            if add_resp.status_code in (200, 201):
                total_added += len(batch)
            else:
                return {
                    "success": False,
                    "error": f"Added {total_added} tracks, but failed adding remaining batch: {add_resp.text}",
                    "playlist_name": name,
                    "playlist_url": playlist_url,
                    "track_count": total_added
                }

        return {
            "success": True,
            "playlist_name": name,
            "playlist_url": playlist_url,
            "track_count": total_added
        }
