import re
import time
import requests
from requests.adapters import HTTPAdapter
import concurrent.futures
from typing import Dict, Any, Optional, Tuple, List
from difflib import SequenceMatcher

DEEZER_SEARCH_URL = "https://api.deezer.com/search"
DEEZER_TRACK_URL = "https://api.deezer.com/track"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
SONGLINK_API_URL = "https://api.song.link/v1-alpha.1/links"

# Persistent HTTP connection session with connection pooling
_HTTP = requests.Session()
_adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=1)
_HTTP.mount("https://", _adapter)
_HTTP.mount("http://", _adapter)

_ISRC_CACHE: Dict[str, Optional[str]] = {}
_SONGLINK_CACHE: Dict[str, Dict[str, Any]] = {}
_CANDIDATES_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def _clean_string(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\.(mp3|flac|m4a|wav|aac|ogg|opus|alac)$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^\d+[\s\.\-_]+", "", s)
    s = re.sub(r"\s*[\(\[\{](?:official|audio|video|remaster|remastered|explicit|deluxe|version|edit|bonus|single|album|extended|mix)[^\)\]\}]*[\)\]\}]\s*", " ", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def extract_artist_variations(artist: str) -> List[str]:
    if not artist:
        return []
    cleaned = _clean_string(artist)
    variations = []
    if cleaned:
        variations.append(cleaned)
    parts = re.split(r"\s*(?:[•/;,]|(?:\s+&\s+)|\s+(?:feat|ft|vs|with)\.?\s+)\s*", artist, flags=re.IGNORECASE)
    for p in parts:
        p_c = _clean_string(p)
        if p_c and p_c not in variations:
            variations.append(p_c)
    return variations or [artist.strip()]


def extract_title_variations(title: str) -> List[str]:
    if not title:
        return []
    raw = title.strip()
    variations = [raw]
    
    t_no_feat = re.sub(r"\s*[\(\[\{](?:feat|ft|with|featuring)[^\)\]\}]*[\)\]\}]\s*", " ", raw, flags=re.IGNORECASE).strip()
    t_no_feat = re.sub(r"\s+(?:feat|ft|with)\.?\s+.*$", "", t_no_feat, flags=re.IGNORECASE).strip()
    if t_no_feat and t_no_feat not in variations:
        variations.append(t_no_feat)

    t_clean = _clean_string(raw)
    if t_clean and t_clean not in variations:
        variations.append(t_clean)

    if "/" in raw:
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        for p in parts:
            p_clean = re.sub(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*", "", p).strip()
            if p_clean and p_clean not in variations:
                variations.append(p_clean)

    return variations


class SmartResolver:
    """
    Blazing-fast Multi-Tier Open Catalog Resolver with connection pooling & parallel dispatch.
    """

    @classmethod
    def get_candidates(cls, artist: str, title: str, album: str = "") -> List[Dict[str, Any]]:
        cache_key = f"{artist.lower().strip()}:::{title.lower().strip()}:::{album.lower().strip()}"
        if cache_key in _CANDIDATES_CACHE:
            return _CANDIDATES_CACHE[cache_key]

        art_vars = extract_artist_variations(artist)
        tit_vars = extract_title_variations(title)
        
        seen_ids = set()
        candidates: List[Dict[str, Any]] = []

        # Parallel query execution for Deezer and iTunes
        def fetch_deezer(q: str):
            try:
                resp = _HTTP.get(DEEZER_SEARCH_URL, params={"q": q, "limit": 4}, timeout=3)
                if resp.status_code == 200:
                    return resp.json().get("data", [])
            except Exception:
                pass
            return []

        def fetch_itunes(q: str):
            try:
                resp = _HTTP.get(ITUNES_SEARCH_URL, params={"term": q, "entity": "song", "limit": 3}, timeout=3)
                if resp.status_code == 200:
                    return resp.json().get("results", [])
            except Exception:
                pass
            return []

        q_primary = f"{art_vars[0]} {tit_vars[0]}".strip()
        q_alt = f"{art_vars[0]} {tit_vars[1]}".strip() if len(tit_vars) > 1 else ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            fut_dz = executor.submit(fetch_deezer, q_primary)
            fut_it = executor.submit(fetch_itunes, q_primary)
            fut_dz_alt = executor.submit(fetch_deezer, q_alt) if q_alt else None

            dz_results = fut_dz.result()
            it_results = fut_it.result()
            dz_alt_results = fut_dz_alt.result() if fut_dz_alt else []

        # Process Deezer items
        for trk in (dz_results + dz_alt_results):
            tid = f"dz_{trk.get('id')}"
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            album_info = trk.get("album", {})
            candidates.append({
                "id": tid,
                "raw_id": trk.get("id"),
                "source": "deezer",
                "name": trk.get("title", ""),
                "artists": trk.get("artist", {}).get("name", ""),
                "album": album_info.get("title", ""),
                "album_art": album_info.get("cover_big") or album_info.get("cover_medium", ""),
                "album_type": "album",
                "album_artists": trk.get("artist", {}).get("name", ""),
                "popularity": int(trk.get("rank", 0) / 10000) if trk.get("rank") else 75,
                "duration_ms": int(trk.get("duration", 0) * 1000),
                "preview_url": trk.get("preview", ""),
                "deezer_id": trk.get("id"),
                "isrc": None
            })

        # Process iTunes items
        for trk in it_results:
            tid = f"itunes_{trk.get('trackId')}"
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            art = trk.get("artworkUrl100", "")
            art_hi = art.replace("100x100bb.jpg", "600x600bb.jpg") if art else ""
            candidates.append({
                "id": tid,
                "raw_id": trk.get("trackId"),
                "source": "itunes",
                "name": trk.get("trackName", ""),
                "artists": trk.get("artistName", ""),
                "album": trk.get("collectionName", ""),
                "album_art": art_hi,
                "album_type": "album",
                "album_artists": trk.get("artistName", ""),
                "popularity": 80,
                "duration_ms": int(trk.get("trackTimeMillis", 0)),
                "preview_url": trk.get("previewUrl", ""),
                "itunes_url": trk.get("trackViewUrl", ""),
                "isrc": None
            })

        # Fast ISRC extraction for top candidate
        if candidates and candidates[0].get("deezer_id"):
            try:
                dz_id = candidates[0]["deezer_id"]
                t_resp = _HTTP.get(f"{DEEZER_TRACK_URL}/{dz_id}", timeout=2.5)
                if t_resp.status_code == 200:
                    isrc = t_resp.json().get("isrc")
                    if isrc:
                        candidates[0]["isrc"] = isrc
            except Exception:
                pass

        _CANDIDATES_CACHE[cache_key] = candidates[:8]
        return candidates[:8]

    @classmethod
    def resolve_cross_platform_links(cls, track_url: str) -> Dict[str, str]:
        if not track_url:
            return {}

        if track_url in _SONGLINK_CACHE:
            return _SONGLINK_CACHE[track_url]

        links: Dict[str, str] = {}
        try:
            resp = _HTTP.get(SONGLINK_API_URL, params={"url": track_url, "userCountry": "US"}, timeout=3)
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
