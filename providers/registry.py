from typing import Dict
from providers import BaseProvider
from providers.spotify_provider import SpotifyProvider
from providers.ytmusic_provider import YouTubeMusicProvider
from providers.tidal_provider import TidalProvider

PROVIDERS: Dict[str, BaseProvider] = {
    "spotify": SpotifyProvider(),
    "ytmusic": YouTubeMusicProvider(),
    "tidal": TidalProvider(),
}

def get_provider(name: str) -> BaseProvider:
    return PROVIDERS.get(name.lower(), PROVIDERS["spotify"])
