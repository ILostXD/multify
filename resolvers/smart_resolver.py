import re
import time
import unicodedata
import requests
from requests.adapters import HTTPAdapter
from typing import Dict, Any, Optional, Tuple, List
from difflib import SequenceMatcher

DEEZER_SEARCH_URL = "https://api.deezer.com/search"
DEEZER_TRACK_URL = "https://api.deezer.com/track"
SONGLINK_API_URL = "https://api.song.link/v1-alpha.1/links"

_HTTP = requests.Session()
_adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25, max_retries=2)
_HTTP.mount("https://", _adapter)
_HTTP.mount("http://", _adapter)

_ISRC_CACHE: Dict[str, Optional[str]] = {}
_SONGLINK_CACHE: Dict[str, Dict[str, Any]] = {}
_CANDIDATES_CACHE: Dict[str, List[Dict[str, Any]]] = {}

PENALTY_KEYWORDS = [
    "tribute", "karaoke", "instrumental", "cover", "slowed", "sped up", "speed up",
    "remix", "orchestral", "lullaby", "babies go", "female version", "male version",
    "piano version", "violin", "acoustic version", "parody", "in the style of",
    "originally performed"
]


def _normalize_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\.(mp3|flac|m4a|wav|aac|ogg|opus|alac)$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^\d+[\s\.\-_]+", "", s)
    s = re.sub(r"\s*[\(\[\{](?:official|audio|video|remaster|remastered|explicit|deluxe|version|edit|bonus|single|album|extended|mix)[^\)\]\}]*[\)\]\}]\s*", " ", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def extract_artist_variations(artist: str) -> List[str]:
    if not artist:
        return []
    
    norm = _normalize_text(artist)
    variations = [artist.strip()]
    if norm and norm not in variations:
        variations.append(norm)

    # Strip "The " prefix
    if norm.lower().startswith("the "):
        no_the = norm[4:].strip()
        if no_the and no_the not in variations:
            variations.append(no_the)

    leet = re.sub(r"\$", "s", norm, flags=re.IGNORECASE)
    clean_punct = re.sub(r"[\$\.]", "", norm)
    for v in [leet, clean_punct]:
        v_s = v.strip()
        if v_s and v_s not in variations:
            variations.append(v_s)

    parts = re.split(r"\s*(?:[•/;,]|(?:\s+&\s+)|\s+(?:feat|ft|vs|with)\.?\s+)\s*", norm, flags=re.IGNORECASE)
    for p in parts:
        p_c = p.strip()
        if p_c and p_c not in variations:
            variations.append(p_c)
        if p_c.lower().startswith("the "):
            p_no_the = p_c[4:].strip()
            if p_no_the and p_no_the not in variations:
                variations.append(p_no_the)
        p_clean = re.sub(r"[\$\.]", "", p_c).strip()
        if p_clean and p_clean not in variations:
            variations.append(p_clean)

    return variations


def extract_title_variations(title: str) -> List[str]:
    if not title:
        return []
    norm = _normalize_text(title)
    variations = [title.strip()]
    if norm and norm not in variations:
        variations.append(norm)

    leet_s = re.sub(r"\$", "s", norm, flags=re.IGNORECASE)
    if leet_s and leet_s not in variations:
        variations.append(leet_s)

    leet_00 = re.sub(r"oo", "00", norm, flags=re.IGNORECASE)
    if leet_00 and leet_00 not in variations:
        variations.append(leet_00)

    leet_oo = re.sub(r"00", "oo", norm, flags=re.IGNORECASE)
    if leet_oo and leet_oo not in variations:
        variations.append(leet_oo)

    t_no_feat = re.sub(r"\s*[\(\[\{](?:feat|ft|with|featuring)[^\)\]\}]*[\)\]\}]\s*", " ", norm, flags=re.IGNORECASE).strip()
    t_no_feat = re.sub(r"\s+(?:feat|ft|with)\.?\s+.*$", "", t_no_feat, flags=re.IGNORECASE).strip()
    if t_no_feat and t_no_feat not in variations:
        variations.append(t_no_feat)

    if "/" in title or "/" in norm:
        parts = [p.strip() for p in norm.split("/") if p.strip()]
        for p in parts:
            p_clean = re.sub(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]\s*", "", p).strip()
            if p_clean and p_clean not in variations:
                variations.append(p_clean)

    return variations


def score_candidate(cand: Dict[str, Any], target_artist: str, target_title: str, target_album: str = "") -> Tuple[float, str]:
    t_art = _normalize_text(target_artist).lower()
    t_tit = _normalize_text(target_title).lower()
    t_alb = _normalize_text(target_album).lower()

    c_art = _normalize_text(cand.get("artists", "")).lower()
    c_tit = _normalize_text(cand.get("name", "")).lower()
    c_alb = _normalize_text(cand.get("album", "")).lower()
    c_full_title = cand.get("name", "").lower()

    score = 0.0
    badge = "Match"

    # 1. Artist Matching (0 - 50 pts)
    artist_exact = False
    if t_art and c_art:
        t_no_the = t_art[4:] if t_art.startswith("the ") else t_art
        c_no_the = c_art[4:] if c_art.startswith("the ") else c_art
        if t_art == c_art or t_no_the == c_no_the:
            score += 50.0
            artist_exact = True
        elif t_art in c_art or c_art in t_art or t_no_the in c_no_the or c_no_the in t_no_the:
            score += 42.0
            artist_exact = True
        else:
            sub_artists = [_normalize_text(a).lower() for a in re.split(r"[•/;,]|\s+&\s+|\s+(?:feat|ft|vs|with)\.?\s+", target_artist, flags=re.IGNORECASE) if a.strip()]
            if any(sa and (sa in c_art or c_art in sa) for sa in sub_artists):
                score += 40.0
                artist_exact = True
            else:
                score += 0.0
    elif not t_art:
        score += 25.0

    # 2. Title Matching (0 - 40 pts)
    title_exact = False
    if t_tit and c_tit:
        t_tit_norm = t_tit.replace("00", "oo").replace("$", "s")
        c_tit_norm = c_tit.replace("00", "oo").replace("$", "s")
        if t_tit == c_tit or t_tit_norm == c_tit_norm:
            score += 40.0
            title_exact = True
        elif c_tit.startswith(t_tit) or t_tit.startswith(c_tit) or c_tit_norm.startswith(t_tit_norm):
            score += 32.0
        else:
            ratio = SequenceMatcher(None, t_tit_norm, c_tit_norm).ratio()
            score += ratio * 30.0

    # 3. Album Matching (0 - 15 pts)
    album_matched = False
    if t_alb and c_alb:
        if t_alb == c_alb or t_alb in c_alb or c_alb in t_alb:
            score += 15.0
            album_matched = True

    # 4. Penalty for Covers / Tributes / Karaoke if not in original title
    t_raw_lower = target_title.lower()
    is_penalty = False
    for kw in PENALTY_KEYWORDS:
        if kw in c_full_title and kw not in t_raw_lower:
            score -= 45.0
            is_penalty = True
            if "cover" in kw or "lullaby" in kw or "babies go" in kw or "tribute" in kw:
                badge = "Cover"
            elif "karaoke" in kw:
                badge = "Karaoke"
            elif "instrumental" in kw:
                badge = "Instrumental"
            elif "remix" in kw:
                badge = "Remix"
            else:
                badge = "Alternate"
            break

    # 5. Popularity boost (0 - 5 pts)
    pop = cand.get("popularity", 0)
    score += (min(pop, 1000000) / 1000000.0) * 5.0

    # 6. Badge determination
    if not is_penalty:
        if artist_exact and title_exact and album_matched:
            badge = "Exact Match"
        elif artist_exact and title_exact:
            badge = "Studio Track"
        elif artist_exact:
            badge = "Artist Match"
        else:
            badge = "Partial Match"

    return score, badge


def _safe_fetch_deezer(q: str, retries: int = 2) -> List[Dict[str, Any]]:
    for attempt in range(retries + 1):
        try:
            resp = _HTTP.get(DEEZER_SEARCH_URL, params={"q": q, "limit": 6}, timeout=4.0)
            if resp.status_code == 200:
                data = resp.json()
                if "error" in data:
                    err_code = data["error"].get("code", 0)
                    if err_code == 4 and attempt < retries:
                        time.sleep(0.35)
                        continue
                    return []
                return data.get("data", [])
            elif resp.status_code == 429 and attempt < retries:
                time.sleep(0.35)
                continue
        except Exception:
            if attempt < retries:
                time.sleep(0.2)
                continue
    return []


class SmartResolver:
    """
    Intelligent Adaptive Multi-Tier Open Catalog Resolver with Multi-Query Pooling & Scoring.
    """

    @classmethod
    def get_candidates(cls, artist: str, title: str, album: str = "") -> List[Dict[str, Any]]:
        cache_key = f"{artist.lower().strip()}:::{title.lower().strip()}:::{album.lower().strip()}"
        if cache_key in _CANDIDATES_CACHE:
            return _CANDIDATES_CACHE[cache_key]

        art_vars = extract_artist_variations(artist)
        tit_vars = extract_title_variations(title)
        
        seen_ids = set()
        raw_candidates: List[Dict[str, Any]] = []

        # Construct prioritized query set
        queries = [
            f"{art_vars[0]} {tit_vars[0]}".strip(),
            f'artist:"{art_vars[0]}" track:"{tit_vars[0]}"',
            tit_vars[0].strip()
        ]
        if len(art_vars) > 1:
            queries.append(f"{art_vars[1]} {tit_vars[0]}".strip())
        if len(tit_vars) > 1:
            queries.append(f"{art_vars[0]} {tit_vars[1]}".strip())

        # Execute queries in order, aggregating all distinct track hits
        for q in queries:
            dz_data = _safe_fetch_deezer(q)
            for trk in dz_data:
                tid = f"dz_{trk.get('id')}"
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    album_info = trk.get("album", {})
                    raw_candidates.append({
                        "id": tid,
                        "raw_id": trk.get("id"),
                        "source": "deezer",
                        "name": trk.get("title", ""),
                        "artists": trk.get("artist", {}).get("name", ""),
                        "album": album_info.get("title", ""),
                        "album_art": album_info.get("cover_big") or album_info.get("cover_medium", ""),
                        "album_type": "album",
                        "album_artists": trk.get("artist", {}).get("name", ""),
                        "popularity": trk.get("rank", 0),
                        "duration_ms": int(trk.get("duration", 0) * 1000),
                        "preview_url": trk.get("preview", ""),
                        "deezer_id": trk.get("id"),
                        "isrc": None
                    })
            if len(raw_candidates) >= 10:
                break

        # ── Composite Scoring, Ranking, and Match Badging ─────────────────────
        scored_candidates = []
        for c in raw_candidates:
            c_score, c_badge = score_candidate(c, artist, title, album)
            c["match_score"] = c_score
            c["match_badge"] = c_badge
            scored_candidates.append((c_score, c))

        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        ranked = [c for _, c in scored_candidates]

        # Fast ISRC & Songlink Cross-Bridge for top candidate
        if ranked and ranked[0].get("deezer_id"):
            try:
                dz_id = ranked[0]["deezer_id"]
                t_resp = _HTTP.get(f"{DEEZER_TRACK_URL}/{dz_id}", timeout=2.5)
                if t_resp.status_code == 200:
                    isrc = t_resp.json().get("isrc")
                    if isrc:
                        ranked[0]["isrc"] = isrc
            except Exception:
                pass

        final_candidates = ranked[:8]
        _CANDIDATES_CACHE[cache_key] = final_candidates
        return final_candidates

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
