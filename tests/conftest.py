# ABOUTME: Provides an offline transport seam for the installed tidalapi library.
# ABOUTME: Records synthetic test requests without credentials or any service connections.

from copy import deepcopy
from types import SimpleNamespace

import pytest
import requests
import tidalapi


@pytest.fixture
def local_tidal(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("test attempted a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", no_network)
    session = tidalapi.Session()
    documents = {}
    items = {}
    events = []
    catalog = []

    def request(method, path, params=None, **kwargs):
        events.append({"method": method, "path": path, "params": params, **deepcopy(kwargs)})
        if path == "search":
            result = {key: {"items": []} for key in ("artists", "albums", "videos", "playlists")}
            result.update(tracks={"items": catalog}, topHit=None)
        elif path == "users/123/playlists":
            result = {"items": list(documents.values())}
        elif path.endswith("/create-playlist"):
            playlist_id = str(len(documents) + 1)
            result = {
                "uuid": playlist_id,
                "title": params["name"],
                "description": params["description"],
                "numberOfTracks": 0,
                "numberOfVideos": 0,
                "duration": 0,
                "type": "USER",
                "image": None,
                "squareImage": None,
                "promotedArtists": [],
                "creator": {"id": 123},
                "publicPlaylist": True,
            }
            documents[playlist_id] = result
            items[playlist_id] = []
            result = {"data": result}
        elif path.startswith("playlists/") or path.startswith("my-collection/playlists/"):
            # Visibility endpoints and v1 playlist endpoints both contain the ID.
            playlist_id = next(part for part in path.split("/") if part in documents)
            result = documents[playlist_id]
            if path.endswith("/set-private"):
                result["publicPlaylist"] = False
            elif path.endswith("/set-public"):
                result["publicPlaylist"] = True
            elif method == "POST" and path.endswith("/items"):
                data = kwargs["data"]
                added = data["trackIds"].split(",")
                start = data["toIndex"]
                items[playlist_id][start:start] = added
                result["numberOfTracks"] = len(items[playlist_id])
                result = {"addedItemIds": added}
            elif method != "GET":
                raise AssertionError(f"unexpected local request: {method} {path}")
        else:
            raise AssertionError(f"unexpected local request: {method} {path}")
        return SimpleNamespace(
            headers={"etag": "local-etag"}, ok=True, json=lambda: deepcopy(result)
        )

    monkeypatch.setattr(session.request, "request", request)
    user = object.__new__(tidalapi.LoggedInUser)
    user.id = 123
    user.session = session
    user.request = session.request
    user.playlist = session.playlist()
    session.user = user
    return SimpleNamespace(
        session=session, documents=documents, items=items, events=events, catalog=catalog
    )


def catalog_track(track_id=11):
    """Minimal synthetic wire metadata consumed by the installed Track parser."""
    return {
        "id": track_id,
        "title": "Song",
        "artists": [{"id": 101, "name": "Lead"}],
        "album": None,
        "duration": 60,
        "explicit": False,
        "allowStreaming": True,
        "streamReady": True,
        "stemReady": False,
        "djReady": False,
        "adSupportedStreamReady": False,
        "trackNumber": 1,
        "volumeNumber": 1,
        "popularity": 0,
        "isrc": "TEST00000001",
    }


def spotify_input(count):
    track = {
        "type": "track",
        "uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        "name": "Song",
        "artists": [{"name": "Lead"}],
        "duration_ms": 60000,
        "explicit": False,
    }
    return SimpleNamespace(
        playlist=lambda *args, **kwargs: {"name": "Test", "description": "Description"},
        playlist_items=lambda *args, **kwargs: {"items": [{"item": track}] * count, "next": None},
    )
