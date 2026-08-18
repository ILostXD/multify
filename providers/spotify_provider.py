import base64
import re
import time
import requests
from typing import List, Dict, Any, Optional, Tuple
from providers import BaseProvider
from resolvers.smart_resolver import SmartResolver, _clean_string

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"


class SpotifyProvider(BaseProvider):
    name = "spotify"
    display_name = "Spotify"
    brand_color = "#1db954"
    icon = "disc"

    def _get_fresh_token(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> Optional[str]:
        """Return a valid, non-expired access token, automatically refreshing if needed."""
        token = session_data.get("access_token") or config.get("spotify_access_token")
        expires_at = session_data.get("token_expires_at") or config.get("spotify_token_expires_at", 0)
        refresh_token = session_data.get("refresh_token") or config.get("spotify_refresh_token")

        if token and time.time() < (expires_at - 60):
            return token

        if refresh_token:
            client_id = config.get("spotify_client_id")
            client_secret = config.get("spotify_client_secret")
            if client_id and client_secret:
                try:
                    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                    resp = requests.post(
                        SPOTIFY_TOKEN_URL,
                        headers={"Authorization": f"Basic {auth_header}"},
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": refresh_token,
                        },
                        timeout=10
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        new_token = data["access_token"]
                        new_expires = time.time() + data.get("expires_in", 3600) - 120
                        session_data["access_token"] = new_token
                        session_data["token_expires_at"] = new_expires
                        if data.get("refresh_token"):
                            session_data["refresh_token"] = data["refresh_token"]
                        return new_token
                except Exception:
                    pass

        return token

    def is_authenticated(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> bool:
        return bool(self._get_fresh_token(config, session_data) or config.get("spotify_refresh_token"))

    def get_auth_header(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> Optional[Dict[str, str]]:
        token = self._get_fresh_token(config, session_data)
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    def search(self, artist: str, title: str, album: str, config: Dict[str, Any], session_data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        headers = self.get_auth_header(config, session_data)
        if not headers:
            return [], None

        results = []
        seen_ids = set()

        clean_title = _clean_string(title) or title
        clean_artist = _clean_string(artist) or artist
        clean_album = _clean_string(album) if album else ""

        # ── Tier 1: Smart Open Catalog & ISRC Resolution ─────────────────────
        try:
            meta = SmartResolver.get_isrc_and_metadata(clean_artist, clean_title, clean_album)
            isrc = meta.get("isrc")
            if isrc:
                isrc_hits, retry = self._spotify_search(f"isrc:{isrc}", headers, seen_ids)
                if retry:
                    return [], retry
                if isrc_hits:
                    # Exact ISRC match found on Spotify!
                    results += isrc_hits
                    return results, None

            # Check Songlink cross-platform bridge if Deezer ID was found
            if meta.get("deezer_id"):
                links = SmartResolver.resolve_cross_platform_links(f"https://www.deezer.com/track/{meta['deezer_id']}")
                sp_id = links.get("spotify_id")
                if sp_id and sp_id not in seen_ids:
                    try:
                        trk_resp = requests.get(f"{SPOTIFY_API_BASE}/tracks/{sp_id}", headers=headers, timeout=6)
                        if trk_resp.status_code == 200:
                            t = trk_resp.json()
                            seen_ids.add(sp_id)
                            album_info = t.get("album", {})
                            images = album_info.get("images", [])
                            art = images[0]["url"] if images else meta.get("artwork_url", "")
                            album_artists_str = ", ".join(a["name"] for a in album_info.get("artists", []))
                            results.append({
                                "id": sp_id,
                                "uri": t["uri"],
                                "name": t["name"],
                                "artists": ", ".join(a["name"] for a in t.get("artists", [])),
                                "album": album_info.get("name", ""),
                                "album_art": art,
                                "album_type": album_info.get("album_type", "album"),
                                "album_artists": album_artists_str,
                                "popularity": t.get("popularity", 90),
                                "duration_ms": t.get("duration_ms", 0),
                                "preview_url": t.get("preview_url") or meta.get("preview_url")
                            })
                            return results, None
                    except Exception:
                        pass
        except Exception:
            pass

        # ── Tier 2: Spotify Relevance Query (Single Shot) ────────────────────
        if not results and clean_artist and clean_title:
            q1 = f"{clean_artist} {clean_title}".strip()
            hits, retry = self._spotify_search(q1, headers, seen_ids)
            if retry:
                return [], retry
            results += hits

        # ── Tier 3: Strict & Album Fallbacks (Only if 0 results) ─────────────
        if not results and clean_artist and clean_album and clean_title:
            q2 = f"{clean_artist} {clean_album} {clean_title}".strip()
            hits, retry = self._spotify_search(q2, headers, seen_ids)
            if retry:
                return [], retry
            results += hits

        if not results and clean_artist and clean_title:
            q3 = f'track:"{clean_title}" artist:"{clean_artist}"'
            hits, retry = self._spotify_search(q3, headers, seen_ids)
            if retry:
                return [], retry
            results += hits
        elif not results and clean_title:
            hits, retry = self._spotify_search(clean_title, headers, seen_ids)
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
            return {"success": False, "error": "Not authenticated with Spotify. Please re-login in Settings."}

        # Filter and validate Spotify track URIs
        valid_uris = [u for u in track_uris if u and (u.startswith("spotify:track:") or u.startswith("spotify:episode:"))]
        if not valid_uris:
            return {"success": False, "error": "No valid Spotify track matches found to add to playlist."}

        # 1. Create Playlist using /me/playlists (modern endpoint)
        payloads_to_try = [
            {"name": name, "public": False, "description": "Imported via Multify"},
            {"name": name, "public": True, "description": "Imported via Multify"},
            {"name": name, "description": "Imported via Multify"}
        ]

        create_resp = None
        for payload in payloads_to_try:
            create_resp = requests.post(
                f"{SPOTIFY_API_BASE}/me/playlists",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
                timeout=12
            )
            if create_resp.status_code in (200, 201):
                break

        # Fallback to /users/{user_id}/playlists if /me failed
        if not create_resp or create_resp.status_code not in (200, 201):
            me_resp = requests.get(f"{SPOTIFY_API_BASE}/me", headers=headers, timeout=10)
            if me_resp.status_code == 200:
                user_id = me_resp.json().get("id")
                if user_id:
                    create_resp = requests.post(
                        f"{SPOTIFY_API_BASE}/users/{user_id}/playlists",
                        headers={**headers, "Content-Type": "application/json"},
                        json={"name": name, "public": False, "description": "Imported via Multify"},
                        timeout=12
                    )

        if not create_resp or create_resp.status_code not in (200, 201):
            status_code = create_resp.status_code if create_resp else 500
            err_msg = ""
            try:
                err_data = create_resp.json().get("error", {})
                err_msg = err_data.get("message", create_resp.text if create_resp else "Unknown error")
            except Exception:
                err_msg = create_resp.text if create_resp else "Unknown error"

            if status_code == 403:
                return {
                    "success": False,
                    "error": (
                        f"Spotify 403 Forbidden ({err_msg}). "
                        "If your Spotify Developer App is in Development Mode, you MUST add your Spotify account email "
                        "under 'User Management' in the Spotify Developer Dashboard (developer.spotify.com/dashboard), "
                        "or re-login to Spotify to refresh your authorization scopes."
                    )
                }
            elif status_code == 401:
                return {
                    "success": False,
                    "error": "Spotify session expired (401). Please click Re-login to Spotify in Settings."
                }
            else:
                return {"success": False, "error": f"Failed to create Spotify playlist ({status_code}): {err_msg}"}

        pdata = create_resp.json()
        playlist_id = pdata.get("id")
        playlist_url = pdata.get("external_urls", {}).get("spotify", "")

        # 2. Add tracks in batches of 100 using /items endpoint with fallback to /tracks
        total_added = 0
        batch_size = 100
        for i in range(0, len(valid_uris), batch_size):
            batch = valid_uris[i:i + batch_size]
            
            # Try /items endpoint first (2026 modern spec)
            add_resp = requests.post(
                f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items",
                headers={**headers, "Content-Type": "application/json"},
                json={"uris": batch},
                timeout=15
            )
            
            # Fallback to /tracks endpoint if /items is not supported on older API proxy
            if add_resp.status_code not in (200, 201):
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
                    "error": f"Created playlist '{name}', but failed adding track batch: {add_resp.text}",
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
