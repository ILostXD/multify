import base64
import re
import time
import requests
from typing import List, Dict, Any, Optional, Tuple
from providers import BaseProvider
from resolvers.smart_resolver import SmartResolver, extract_artist_variations, extract_title_variations

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
        
        # 1. Fetch Multi-Option Candidates via SmartResolver (Deezer + iTunes + ISRC)
        open_candidates = SmartResolver.get_candidates(artist, title, album)
        
        results = []
        seen_ids = set()

        # If Spotify is authenticated, attempt fast precision lookup on Spotify
        rate_limit_hit = False
        if headers:
            # Check ISRC on top candidate
            top_isrc = open_candidates[0].get("isrc") if open_candidates else None
            if top_isrc:
                isrc_hits, retry = self._spotify_search(f"isrc:{top_isrc}", headers, seen_ids)
                if isrc_hits:
                    results += isrc_hits
                elif retry:
                    rate_limit_hit = True

            # If no ISRC hits and not rate limited, try single relevance query
            if not results and not rate_limit_hit:
                art_vars = extract_artist_variations(artist)
                tit_vars = extract_title_variations(title)
                q = f"{art_vars[0]} {tit_vars[0]}".strip()
                hits, retry = self._spotify_search(q, headers, seen_ids)
                if hits:
                    results += hits
                elif retry:
                    rate_limit_hit = True

        # 2. Integrate and format open candidates (providing multiple dropdown options)
        for c in open_candidates:
            cid = c.get("id") or str(c.get("raw_id"))
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            
            # Ensure every candidate has a distinct, valid URI for React dropdown selection
            uri_val = c.get("uri")
            if not uri_val:
                if c.get("isrc"):
                    uri_val = f"spotify:track:{c['isrc']}"
                elif c.get("raw_id"):
                    uri_val = f"spotify:track:{c['source']}_{c['raw_id']}"
                else:
                    uri_val = f"spotify:track:{cid}"

            results.append({
                "id": cid,
                "uri": uri_val,
                "name": c.get("name", title),
                "artists": c.get("artists", artist),
                "album": c.get("album", album or ""),
                "album_art": c.get("album_art", ""),
                "album_type": c.get("album_type", "album"),
                "album_artists": c.get("album_artists", artist),
                "popularity": c.get("popularity", 75),
                "duration_ms": c.get("duration_ms", 0),
                "preview_url": c.get("preview_url", "")
            })

        if not results:
            return [], None

        return results[:8], None

    def _spotify_search(self, query: str, headers: Dict[str, str], seen_ids: set) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        url = f"{SPOTIFY_API_BASE}/search"
        params = {"q": query, "type": "track", "limit": 5}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=8)
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

        # Resolve URIs (handling 22-char Spotify IDs, episodes, and ISRC/open resolutions)
        resolved_uris = []
        for u in track_uris:
            if not u or not isinstance(u, str):
                continue
            u = u.strip()
            # Standard Spotify URI: "spotify:track:..." (36 chars) or "spotify:episode:..."
            if (u.startswith("spotify:track:") and len(u) == 36 and "_" not in u) or u.startswith("spotify:episode:"):
                resolved_uris.append(u)
            elif u.startswith("spotify:track:") or u.startswith("isrc:"):
                code = u.replace("spotify:track:", "").replace("isrc:", "").strip()
                if code:
                    try:
                        sp_res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params={"q": f"isrc:{code}", "type": "track", "limit": 1}, timeout=5)
                        if sp_res.status_code == 200:
                            itms = sp_res.json().get("tracks", {}).get("items", [])
                            if itms:
                                resolved_uris.append(itms[0]["uri"])
                    except Exception:
                        pass

        if not resolved_uris:
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
        for i in range(0, len(resolved_uris), batch_size):
            batch = resolved_uris[i:i + batch_size]
            
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
