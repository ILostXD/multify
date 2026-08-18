import re
import time
import requests
from typing import Dict, Any, Optional, Tuple, List
from difflib import SequenceMatcher

DEEZER_SEARCH_URL = "https://api.deezer.com/search"
DEEZER_TRACK_URL = "https://api.deezer.com/track"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
SONGLINK_API_URL = "https://api.song.link/v1-alpha.1/links"

# In-memory LRU-like caches
_ISRC_CACHE: Dict[str, Optional[str]] = {}
_SONGLINK_CACHE: Dict[str, Dict[str, Any]] = {}
_RESOLVER_CACHE: Dict[str, Any] = {}


def _clean_string(s: str) -> str:
    if not s:
        return ""
    # Remove file extensions
    s = re.sub(r"\.(mp3|flac|m4a|wav|aac|ogg|opus|alac)$", "", s, flags=re.IGNORECASE)
    # Remove track numbers at start e.g. "01 - Title" or "01. Title"
    s = re.sub(r"^\d+[\s\.\-_]+", "", s)
    # Remove bracketed noise
    s = re.sub(r"\s*[\(\[\{](?:official|feat|ft|audio|video|remaster|remastered|explicit|deluxe|version|edit|bonus|single|album|extended|mix)[^\)\]\}]*[\)\]\}]\s*", " ", s, flags=re.IGNORECASE)
    # Strip standalone ft./feat.
    s = re.sub(r"\s+(?:feat|ft)\.?\s+.*$", "", s, flags=re.IGNORECASE)
    # Normalize whitespace
    return re.sub(r"\s+", " ", s).strip()


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


class SmartResolver:
    """
    Multi-tier smart resolver that queries open music catalogs (Deezer, iTunes, Songlink)
    to extract ISRCs and cross-platform identifiers, drastically reducing Spotify API calls.
    """

    @classmethod
    def get_isrc_and_metadata(cls, artist: str, title: str, album: str = "") -> Dict[str, Any]:
        """
        Attempts to find the master ISRC and enriched metadata using free public endpoints.
        """
        clean_art = _clean_string(artist)
        clean_tit = _clean_string(title)
        cache_key = f"{clean_art.lower()}:::{clean_tit.lower()}"

        if cache_key in _ISRC_CACHE and _ISRC_CACHE[cache_key]:
            return {"isrc": _ISRC_CACHE[cache_key], "artist": clean_art, "title": clean_tit}

        result: Dict[str, Any] = {
            "isrc": None,
            "artist": clean_art,
            "title": clean_tit,
            "album": album,
            "artwork_url": "",
            "preview_url": "",
            "deezer_id": None,
            "itunes_url": None
        }

        # 1. Query Deezer Open API
        try:
            q = f"{clean_art} {clean_tit}".strip()
            resp = requests.get(DEEZER_SEARCH_URL, params={"q": q, "limit": 4}, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                tracks = data.get("data", [])
                for trk in tracks:
                    dz_title = trk.get("title", "")
                    dz_artist = trk.get("artist", {}).get("name", "")
                    # Match confidence
                    sim_title = _similarity(clean_tit, dz_title)
                    sim_artist = _similarity(clean_art, dz_artist)
                    if (sim_title > 0.6 and sim_artist > 0.5) or (clean_art.lower() in dz_artist.lower() and sim_title > 0.5):
                        dz_id = trk.get("id")
                        if dz_id:
                            result["deezer_id"] = dz_id
                            # Fetch full track metadata for ISRC
                            t_resp = requests.get(f"{DEEZER_TRACK_URL}/{dz_id}", timeout=4)
                            if t_resp.status_code == 200:
                                t_data = t_resp.json()
                                isrc = t_data.get("isrc")
                                if isrc:
                                    result["isrc"] = isrc
                                    result["artwork_url"] = t_data.get("album", {}).get("cover_big") or trk.get("album", {}).get("cover_medium", "")
                                    result["preview_url"] = t_data.get("preview", "")
                                    _ISRC_CACHE[cache_key] = isrc
                                    return result
        except Exception:
            pass

        # 2. Query iTunes Search API (fallback for artwork/preview)
        try:
            q = f"{clean_art} {clean_tit}".strip()
            resp = requests.get(ITUNES_SEARCH_URL, params={"term": q, "entity": "song", "limit": 3}, timeout=4)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    top = results[0]
                    result["itunes_url"] = top.get("trackViewUrl")
                    if not result["artwork_url"]:
                        art = top.get("artworkUrl100", "")
                        result["artwork_url"] = art.replace("100x100bb.jpg", "600x600bb.jpg") if art else ""
                    if not result["preview_url"]:
                        result["preview_url"] = top.get("previewUrl", "")
        except Exception:
            pass

        _ISRC_CACHE[cache_key] = result.get("isrc")
        return result

    @classmethod
    def resolve_cross_platform_links(cls, track_url: str) -> Dict[str, str]:
        """
        Queries Songlink to get direct links for Spotify, Tidal, YouTube Music.
        """
        if not track_url:
            return {}

        if track_url in _SONGLINK_CACHE:
            return _SONGLINK_CACHE[track_url]

        links: Dict[str, str] = {}
        try:
            resp = requests.get(SONGLINK_API_URL, params={"url": track_url, "userCountry": "US"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                by_plat = data.get("linksByPlatform", {})
                for plat in ["spotify", "tidal", "youtubeMusic", "appleMusic", "deezer"]:
                    if plat in by_plat:
                        links[plat] = by_plat[plat].get("url", "")
                        if plat == "spotify" and by_plat[plat].get("entityUniqueId"):
                            entity_id = by_plat[plat]["entityUniqueId"]
                            if "::" in entity_id:
                                track_id = entity_id.split("::")[-1]
                                links["spotify_uri"] = f"spotify:track:{track_id}"
                                links["spotify_id"] = track_id
        except Exception:
            pass

        _SONGLINK_CACHE[track_url] = links
        return links
