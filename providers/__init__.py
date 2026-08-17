from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class BaseProvider(ABC):
    name: str = ""
    display_name: str = ""
    brand_color: str = ""
    icon: str = ""

    @abstractmethod
    def is_authenticated(self, config: Dict[str, Any], session_data: Dict[str, Any]) -> bool:
        """Return True if the provider has valid credentials/tokens."""
        pass

    @abstractmethod
    def search(self, artist: str, title: str, album: str, config: Dict[str, Any], session_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Search for track matches.
        Returns list of dicts with keys:
          - id: str
          - uri: str (or identifier used for adding to playlist)
          - name: str
          - artists: str
          - album: str
          - album_art: str
          - duration_ms: int (optional)
        """
        pass

    @abstractmethod
    def create_playlist(self, name: str, track_uris: List[str], config: Dict[str, Any], session_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a new playlist and add the track_uris.
        Returns dict with:
          - success: bool
          - playlist_name: str
          - playlist_url: str
          - track_count: int
          - error: Optional[str]
        """
        pass
