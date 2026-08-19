import base64
import re
import time
import requests
import concurrent.futures
from typing import List, Dict, Any, Optional, Tuple
from providers import BaseProvider
from resolvers.smart_resolver import SmartResolver, extract_artist_variations, extract_title_variations

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

_ISRC_TO_SPOTIFY_CACHE: Dict[str, str] = {}


class SpotifyProvider(BaseProvider):
    name = "spotify"
    display_name = "Spotify"
    brand_color = "#1db954"
    icon = "disc"

    def _get_fresh_token(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> Optional[str]:
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
                        config["spotify_access_token"] = new_token
                        config["spotify_token_expires_at"] = new_expires
                        if data.get("refresh_token"):
                            session_data["refresh_token"] = data["refresh_token"]
                            config["spotify_refresh_token"] = data["refresh_token"]
                        try:
                            from app import save_config
                            save_config(config)
                        except Exception:
                            pass
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
        # Fast UI search powered by open multi-engine (0 Spotify Quota consumed during UI search!)
        open_candidates = SmartResolver.get_candidates(artist, title, album)
        
        results = []
        seen_ids = set()

        for c in open_candidates:
            cid = c.get("id") or str(c.get("raw_id"))
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            
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
                "preview_url": c.get("preview_url", ""),
                "match_badge": c.get("match_badge", "Match"),
                "match_score": c.get("match_score", 80),
                "isrc": c.get("isrc")
            })

        if not results:
            return [], None

        return results[:8], None

    def create_playlist(self, name: str, track_uris: List[str], config: Dict[str, Any], session_data: Dict[str, Any], track_objects: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        headers = self.get_auth_header(config, session_data)
        if not headers:
            return {"success": False, "error": "Not authenticated with Spotify. Please re-login in Settings."}

        direct_uris = []
        items_to_resolve = []

        # 1. Parse incoming track selections
        if track_objects:
            for obj in track_objects:
                u = (obj.get("uri") or "").strip()
                isrc = (obj.get("isrc") or "").strip()
                art = (obj.get("artist") or obj.get("artists") or "").strip()
                tit = (obj.get("title") or obj.get("name") or "").strip()

                if u.startswith("spotify:track:") and len(u) == 36 and "_" not in u and not u[14:].isupper():
                    direct_uris.append(u)
                elif u.startswith("spotify:episode:"):
                    direct_uris.append(u)
                else:
                    clean_code = isrc or u.replace("spotify:track:", "").replace("isrc:", "").strip()
                    if clean_code.startswith("deezer_") or clean_code.startswith("itunes_"):
                        clean_code = ""
                    items_to_resolve.append({
                        "isrc": clean_code,
                        "artist": art,
                        "title": tit,
                        "query": f"{art} {tit}".strip()
                    })
        else:
            for u in track_uris:
                if not u or not isinstance(u, str):
                    continue
                u = u.strip()
                if u.startswith("spotify:track:") and len(u) == 36 and "_" not in u and not u[14:].isupper():
                    direct_uris.append(u)
                elif u.startswith("spotify:episode:"):
                    direct_uris.append(u)
                else:
                    clean_code = u.replace("spotify:track:", "").replace("isrc:", "").strip()
                    if clean_code.startswith("deezer_") or clean_code.startswith("itunes_"):
                        clean_code = ""
                    items_to_resolve.append({
                        "isrc": clean_code,
                        "artist": "",
                        "title": "",
                        "query": ""
                    })

        # 2. Parallel resolver with rate-limit retry & text fallback
        def resolve_single(item: Dict[str, str]) -> Optional[str]:
            isrc = item.get("isrc", "")
            query = item.get("query", "")
            art = item.get("artist", "")
            tit = item.get("title", "")

            # Check cache
            if isrc and isrc in _ISRC_TO_SPOTIFY_CACHE:
                return _ISRC_TO_SPOTIFY_CACHE[isrc]
            if query and query in _ISRC_TO_SPOTIFY_CACHE:
                return _ISRC_TO_SPOTIFY_CACHE[query]

            # Try ISRC on Spotify
            if isrc:
                for attempt in range(3):
                    try:
                        sp_res = requests.get(
                            f"{SPOTIFY_API_BASE}/search",
                            headers=headers,
                            params={"q": f"isrc:{isrc}", "type": "track", "limit": 1},
                            timeout=5
                        )
                        if sp_res.status_code == 200:
                            itms = sp_res.json().get("tracks", {}).get("items", [])
                            if itms:
                                sp_uri = itms[0]["uri"]
                                _ISRC_TO_SPOTIFY_CACHE[isrc] = sp_uri
                                return sp_uri
                            break # No ISRC match on Spotify, proceed to text search
                        elif sp_res.status_code == 429:
                            wait = int(sp_res.headers.get("Retry-After", "2"))
                            time.sleep(wait + 0.2)
                        elif sp_res.status_code == 401:
                            return None
                        else:
                            break
                    except Exception:
                        time.sleep(0.3)

            # Fallback to Text Search on Spotify
            text_q = query or f"{art} {tit}".strip()
            if text_q:
                for attempt in range(3):
                    try:
                        sp_res = requests.get(
                            f"{SPOTIFY_API_BASE}/search",
                            headers=headers,
                            params={"q": text_q, "type": "track", "limit": 1},
                            timeout=5
                        )
                        if sp_res.status_code == 200:
                            itms = sp_res.json().get("tracks", {}).get("items", [])
                            if itms:
                                sp_uri = itms[0]["uri"]
                                if isrc:
                                    _ISRC_TO_SPOTIFY_CACHE[isrc] = sp_uri
                                _ISRC_TO_SPOTIFY_CACHE[text_q] = sp_uri
                                return sp_uri
                            break
                        elif sp_res.status_code == 429:
                            wait = int(sp_res.headers.get("Retry-After", "2"))
                            time.sleep(wait + 0.2)
                        elif sp_res.status_code == 401:
                            return None
                        else:
                            break
                    except Exception:
                        time.sleep(0.3)

            return None

        # Resolve concurrently with controlled 6-worker pool
        if items_to_resolve:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(resolve_single, items_to_resolve))
                for sp_uri in results:
                    if sp_uri:
                        direct_uris.append(sp_uri)

        # Deduplicate while preserving playlist order
        seen_uris = set()
        resolved_uris = []
        for u in direct_uris:
            if u and u not in seen_uris:
                seen_uris.add(u)
                resolved_uris.append(u)

        if not resolved_uris:
            return {"success": False, "error": "No valid Spotify track matches found to add to playlist. Please re-login to Spotify in Settings."}

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

        if not create_resp or create_resp.status_code not in (200, 201):
            me_resp = requests.get(f"{SPOTIFY_API_BASE}/me", headers=headers, timeout=8)
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
                        "or re-login to Spotify in Settings."
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

        total_added = 0
        batch_size = 100
        for i in range(0, len(resolved_uris), batch_size):
            batch = resolved_uris[i:i + batch_size]
            add_resp = requests.post(
                f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items",
                headers={**headers, "Content-Type": "application/json"},
                json={"uris": batch},
                timeout=15
            )
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
