import sys
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
SPOTIFY_TRACK_URI_RE = re.compile(r"^spotify:track:[0-9a-zA-Z]{22}$")
SPOTIFY_EPISODE_URI_RE = re.compile(r"^spotify:episode:[0-9a-zA-Z]{22}$")

_ISRC_TO_SPOTIFY_CACHE: Dict[str, str] = {}


def pick_best_spotify_item(items: List[Dict[str, Any]], target_artist: str = "", target_title: str = "", target_album: str = "") -> Optional[Dict[str, Any]]:
    """Select the best matching track from Spotify candidates, penalizing random compilations in favor of studio albums/singles."""
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    def score_item(itm: Dict[str, Any]) -> float:
        pop = itm.get("popularity", 50) or 50
        score = float(pop)
        
        album = itm.get("album", {}) or {}
        alb_type = (album.get("album_type") or "").lower()
        alb_name = (album.get("name") or "").lower()
        alb_artists = [a.get("name", "").lower() for a in album.get("artists", []) if a.get("name")]
        
        t_art = (target_artist or "").lower()
        t_tit = (target_title or "").lower()
        t_alb = (target_album or "").lower()
        
        # 1. Heavily penalize compilations & compilation buzzwords
        if alb_type == "compilation":
            score -= 80.0
        
        compilation_keywords = [
            "viral", "tiktok", "hits", "compilation", "best of", "summer", "workout", 
            "star rap", "hyperwave", "top 50", "top 100", "various artists", "party hits",
            "gym", "driving", "throwback", "autumn", "winter", "spring", "vibes 202",
            "greatest hits", "top hits", "soundtrack", "ost", "lofi hits", "car music"
        ]
        if any(kw in alb_name for kw in compilation_keywords):
            score -= 70.0
        if any("various" in a for a in alb_artists):
            score -= 60.0

        # 2. Reward studio albums & singles
        if alb_type == "album":
            score += 40.0
        elif alb_type == "single":
            score += 25.0
            
        # 3. Reward artist match on album
        if any(t_art in a or a in t_art for a in alb_artists if a):
            score += 50.0
        elif t_art and alb_artists:
            first_art = t_art.split(",")[0].strip()
            if any(first_art in a or a in first_art for a in alb_artists if a):
                score += 45.0

        # 4. Reward target album match
        if t_alb and len(t_alb) > 2:
            if t_alb == alb_name:
                score += 100.0
            elif t_alb in alb_name or alb_name in t_alb:
                score += 70.0

        return score

    return max(items, key=score_item)


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

    def init_playlist(self, name: str, config: Dict[str, Any], session_data: Dict[str, Any]) -> Dict[str, Any]:
        headers = self.get_auth_header(config, session_data)
        if not headers:
            return {"success": False, "error": "Not authenticated with Spotify. Please re-login in Settings."}

        # 1. Fetch current user ID
        user_id = None
        for attempt in range(2):
            try:
                me_resp = requests.get(f"{SPOTIFY_API_BASE}/me", headers=headers, timeout=8)
                if me_resp.status_code == 200:
                    user_id = me_resp.json().get("id")
                    break
                elif me_resp.status_code == 401 and attempt == 0:
                    # Token expired -> force refresh
                    session_data["token_expires_at"] = 0
                    config["spotify_token_expires_at"] = 0
                    new_token = self._get_fresh_token(config, session_data)
                    if new_token:
                        headers = {"Authorization": f"Bearer {new_token}"}
                        continue
                else:
                    print(f"[Spotify] /me returned {me_resp.status_code}: {me_resp.text}", file=sys.stderr, flush=True)
                    if me_resp.status_code == 403:
                        return {
                            "success": False,
                            "error": (
                                "Spotify 403 Forbidden. Your Spotify account is not registered under 'User Management' "
                                "in the Spotify Developer Dashboard (developer.spotify.com/dashboard), or permissions are restricted. "
                                "Please add your Spotify email to User Management or re-login in Settings."
                            )
                        }
                    elif me_resp.status_code == 401:
                        return {"success": False, "error": "Spotify session expired. Please click Re-login to Spotify in Settings."}
            except Exception as e:
                print(f"[Spotify] Error fetching /me: {e}", file=sys.stderr, flush=True)

        payloads = [
            {"name": name, "public": False, "description": "Imported via Multify"},
            {"name": name, "public": True, "description": "Imported via Multify"},
            {"name": name, "description": "Imported via Multify"}
        ]

        create_resp = None
        # Try /users/{user_id}/playlists first if user_id is known
        if user_id:
            for p in payloads:
                try:
                    create_resp = requests.post(
                        f"{SPOTIFY_API_BASE}/users/{user_id}/playlists",
                        headers={**headers, "Content-Type": "application/json"},
                        json=p,
                        timeout=10
                    )
                    if create_resp.status_code in (200, 201):
                        break
                except Exception as e:
                    print(f"[Spotify] Error creating playlist via /users: {e}", file=sys.stderr, flush=True)

        # Fallback to /me/playlists
        if not create_resp or create_resp.status_code not in (200, 201):
            for p in payloads:
                try:
                    create_resp = requests.post(
                        f"{SPOTIFY_API_BASE}/me/playlists",
                        headers={**headers, "Content-Type": "application/json"},
                        json=p,
                        timeout=10
                    )
                    if create_resp.status_code in (200, 201):
                        break
                except Exception as e:
                    print(f"[Spotify] Error creating playlist via /me: {e}", file=sys.stderr, flush=True)

        if not create_resp or create_resp.status_code not in (200, 201):
            status_code = create_resp.status_code if create_resp else 500
            err_text = create_resp.text if create_resp else "No response from Spotify"
            print(f"[Spotify] Create playlist failed ({status_code}): {err_text}", file=sys.stderr, flush=True)

            err_msg = ""
            try:
                err_data = create_resp.json().get("error", {})
                if isinstance(err_data, dict):
                    err_msg = err_data.get("message") or err_text
                else:
                    err_msg = str(err_data) or err_text
            except Exception:
                err_msg = err_text

            if status_code == 403:
                return {
                    "success": False,
                    "error": (
                        f"Spotify 403 Forbidden ({err_msg}). "
                        "If your Spotify Developer App is in Development Mode, please add your Spotify account email "
                        "under 'User Management' in the Spotify Developer Dashboard (developer.spotify.com/dashboard), "
                        "or re-login to Spotify in Settings."
                    )
                }
            elif status_code == 401:
                return {"success": False, "error": "Spotify session expired (401). Please click Re-login to Spotify in Settings."}
            else:
                return {"success": False, "error": f"Failed to create Spotify playlist ({status_code}): {err_msg}"}

        pdata = create_resp.json()
        return {
            "success": True,
            "playlist_id": pdata.get("id"),
            "playlist_url": pdata.get("external_urls", {}).get("spotify", ""),
            "playlist_name": name
        }


    def add_batch_to_playlist(self, playlist_id: str, track_objects: List[Dict[str, Any]], config: Dict[str, Any], session_data: Dict[str, Any]) -> Dict[str, Any]:
        headers = self.get_auth_header(config, session_data)
        if not headers:
            return {"success": False, "error": "Not authenticated with Spotify."}

        direct_uris = []
        items_to_resolve = []

        for obj in track_objects:
            u = (obj.get("uri") or "").strip()
            isrc = (obj.get("isrc") or "").strip()
            art = (obj.get("artist") or obj.get("artists") or "").strip()
            tit = (obj.get("title") or obj.get("name") or "").strip()
            alb = (obj.get("album") or obj.get("target_album") or "").strip()

            if (SPOTIFY_TRACK_URI_RE.match(u) or SPOTIFY_EPISODE_URI_RE.match(u)):
                direct_uris.append(u)
            else:
                clean_code = isrc or u.replace("spotify:track:", "").replace("isrc:", "").strip()
                if clean_code.startswith("deezer_") or clean_code.startswith("itunes_") or len(clean_code) > 15:
                    clean_code = ""
                items_to_resolve.append({
                    "isrc": clean_code,
                    "artist": art,
                    "title": tit,
                    "album": alb,
                    "query": f"{art} {tit}".strip()
                })

        def resolve_single(item: Dict[str, str]) -> Optional[str]:
            isrc = item.get("isrc", "")
            query = item.get("query", "")
            art = item.get("artist", "")
            tit = item.get("title", "")
            alb = item.get("album", "")

            cache_key = f"{isrc}_{alb}" if isrc else f"{query}_{alb}"
            if cache_key in _ISRC_TO_SPOTIFY_CACHE:
                return _ISRC_TO_SPOTIFY_CACHE[cache_key]

            if isrc:
                try:
                    sp_res = requests.get(
                        f"{SPOTIFY_API_BASE}/search",
                        headers=headers,
                        params={"q": f"isrc:{isrc}", "type": "track", "limit": 10},
                        timeout=3
                    )
                    if sp_res.status_code == 200:
                        itms = sp_res.json().get("tracks", {}).get("items", [])
                        if itms:
                            best = pick_best_spotify_item(itms, art, tit, alb)
                            if best and best.get("uri"):
                                sp_uri = best["uri"]
                                _ISRC_TO_SPOTIFY_CACHE[cache_key] = sp_uri
                                return sp_uri
                except Exception:
                    pass

            text_q = query or f"{art} {tit}".strip()
            if text_q:
                try:
                    sp_res = requests.get(
                        f"{SPOTIFY_API_BASE}/search",
                        headers=headers,
                        params={"q": text_q, "type": "track", "limit": 10},
                        timeout=3
                    )
                    if sp_res.status_code == 200:
                        itms = sp_res.json().get("tracks", {}).get("items", [])
                        if itms:
                            best = pick_best_spotify_item(itms, art, tit, alb)
                            if best and best.get("uri"):
                                sp_uri = best["uri"]
                                _ISRC_TO_SPOTIFY_CACHE[cache_key] = sp_uri
                                return sp_uri
                except Exception:
                    pass

            return None

        if items_to_resolve:
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(resolve_single, items_to_resolve))
                for sp_uri in results:
                    if sp_uri and (SPOTIFY_TRACK_URI_RE.match(sp_uri) or SPOTIFY_EPISODE_URI_RE.match(sp_uri)):
                        direct_uris.append(sp_uri)

        # Deduplicate while preserving order
        unique_uris = []
        for u in direct_uris:
            if u and u not in unique_uris and (SPOTIFY_TRACK_URI_RE.match(u) or SPOTIFY_EPISODE_URI_RE.match(u)):
                unique_uris.append(u)

        if not unique_uris:
            return {"success": True, "added_count": 0}

        # Add to Spotify playlist (support both /items and /tracks with auto-refresh)
        endpoints = [
            f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/items",
            f"{SPOTIFY_API_BASE}/playlists/{playlist_id}/tracks"
        ]

        add_resp = None
        for ep in endpoints:
            for attempt in range(2):
                try:
                    add_resp = requests.post(
                        ep,
                        headers={**headers, "Content-Type": "application/json"},
                        json={"uris": unique_uris},
                        timeout=10
                    )
                    if add_resp.status_code in (200, 201):
                        return {"success": True, "added_count": len(unique_uris)}
                    elif add_resp.status_code == 401 and attempt == 0:
                        session_data["token_expires_at"] = 0
                        config["spotify_token_expires_at"] = 0
                        new_token = self._get_fresh_token(config, session_data)
                        if new_token:
                            headers = {"Authorization": f"Bearer {new_token}"}
                            continue
                except Exception as e:
                    print(f"[Spotify] Error posting to {ep}: {e}", file=sys.stderr, flush=True)

        if add_resp is not None and add_resp.status_code in (200, 201):
            return {"success": True, "added_count": len(unique_uris)}
        else:
            status = add_resp.status_code if add_resp else 500
            err_body = add_resp.text if add_resp else "No response"
            print(f"[Spotify] Add tracks failed ({status}): {err_body}", file=sys.stderr, flush=True)
            return {"success": False, "error": f"Failed to add tracks: {err_body}"}

    def create_playlist(self, name: str, track_uris: List[str], config: Dict[str, Any], session_data: Dict[str, Any], track_objects: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        init_res = self.init_playlist(name, config, session_data)
        if not init_res.get("success"):
            return init_res

        playlist_id = init_res["playlist_id"]
        playlist_url = init_res["playlist_url"]

        items = track_objects or [{"uri": u} for u in track_uris]
        total_added = 0

        batch_size = 25
        for i in range(0, len(items), batch_size):
            chunk = items[i:i + batch_size]
            batch_res = self.add_batch_to_playlist(playlist_id, chunk, config, session_data)
            if batch_res.get("success"):
                total_added += batch_res.get("added_count", 0)

        return {
            "success": True,
            "playlist_name": name,
            "playlist_url": playlist_url,
            "track_count": total_added
        }
