import unicodedata
import re
import requests
from requests.adapters import HTTPAdapter
import concurrent.futures
from typing import Dict, Any, Optional, Tuple, List
from difflib import SequenceMatcher

DEEZER_SEARCH_URL = "https://api.deezer.com/search"
DEEZER_TRACK_URL = "https://api.deezer.com/track"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
SONGLINK_API_URL = "https://api.song.link/v1-alpha.1/links"

_HTTP = requests.Session()
_adapter = HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=1)
_HTTP.mount("https://", _adapter)
_HTTP.mount("http://", _adapter)

_ISRC_CACHE: Dict[str, Optional[str]] = {}
_SONGLINK_CACHE: Dict[str, Dict[str, Any]] = {}
_CANDIDATES_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def _normalize_text(s: str) -> str:
    if not s:
        return ""
    # Unicode NFKD normalization (converts Ÿ -> Y, é -> e, etc.)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    
    # Strip file extensions & leading track numbers
    s = re.sub(r"\.(mp3|flac|m4a|wav|aac|ogg|opus|alac)$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^\d+[\s\.\-_]+", "", s)
    
    # Strip common noise tags
    s = re.sub(r"\s*[\(\[\{](?:official|audio|video|remaster|remastered|explicit|deluxe|version|edit|bonus|single|album|extended|mix)[^\)\]\}]*[\)\]\}]\s*", " ", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def extract_artist_variations(artist: str) -> List[str]:
    if not artist:
        return []
    
    norm = _normalize_text(artist)
    variations = [artist.strip()]
    if norm and norm not in variations:
        variations.append(norm)

    # Leetspeak / symbol normalization: A$AP -> ASAP / Asap
    leet = re.sub(r"\$", "s", norm, flags=re.IGNORECASE)
    leet_clean = re.sub(r"[\$\.]", "", norm)
    for v in [leet, leet_clean]:
        if v and v not in variations:
            variations.append(v)

    # Split delimiters
    parts = re.split(r"\s*(?:[•/;,]|(?:\s+&\s+)|\s+(?:feat|ft|vs|with)\.?\s+)\s*", norm, flags=re.IGNORECASE)
    for p in parts:
        p_c = p.strip()
        if p_c and p_c not in variations:
            variations.append(p_c)
        # Also add leetspeak variation of sub-artist (e.g. A$AP Rocky -> ASAP Rocky)
        p_leet = re.sub(r"\$", "s", p_c, flags=re.IGNORECASE)
        if p_leet and p_leet not in variations:
            variations.append(p_leet)

    return variations


def extract_title_variations(title: str) -> List[str]:
    if not title:
        return []
    norm = _normalize_text(title)
    variations = [title.strip()]
    if norm and norm not in variations:
        variations.append(norm)

    # Leetspeak: Ca$ino -> Casino
    leet = re.sub(r"\$", "s", norm, flags=re.IGNORECASE)
    if leet and leet not in variations:
        variations.append(leet)

    # Strip (feat. ...)
    t_no_feat = re.sub(r"\s*[\(\[\{](?:feat|ft|with|featuring)[^\)\]\}]*[\)\]\}]\s*", " ", norm, flags=re.IGNORECASE).strip()
    t_no_feat = re.sub(r"\s+(?:feat|ft|with)\.?\s+.*$", "", t_no_feat, flags=re.IGNORECASE).strip()
    if t_no_feat and t_no_feat not in variations:
        variations.append(t_no_feat)

    # Handle dual titles separated by "/" e.g. "911 / Mr. Lonely"
    if "/" in title or "/" in norm:
        parts = [p.strip() for p in norm.split("/") if p.strip()]
        for p in parts:
            p_clean = re.sub(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*", "", p).strip()
            if p_clean and p_clean not in variations:
                variations.append(p_clean)

    return variations


class SmartResolver:
    """
    Intelligent Adaptive Multi-Tier Open Catalog Resolver.
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

        # Generate top query pairs
        queries_to_try = []
        for a in art_vars[:2]:
            for t in tit_vars[:2]:
                q = f"{a} {t}".strip()
                if q and q not in queries_to_try:
                    queries_to_try.append(q)

        # Also add pure title if artist is long
        if tit_vars and len(queries_to_try) < 4:
            queries_to_try.append(tit_vars[0])

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            dz_futures = [executor.submit(fetch_deezer, q) for q in queries_to_try[:3]]
            it_futures = [executor.submit(fetch_itunes, q) for q in queries_to_try[:2]]

            dz_results = []
            for f in dz_futures:
                dz_results.extend(f.result())

            it_results = []
            for f in it_futures:
                it_results.extend(f.result())

        # Process Deezer items
        for trk in dz_results:
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
