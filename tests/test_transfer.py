# ABOUTME: Tests the pure playlist-transfer rules without contacting either service.
# ABOUTME: Covers URI parsing, destination naming, matching, and ordered batching.

import inspect
from datetime import date
from types import SimpleNamespace

import pytest
import tidalapi

from spotify_playlist_to_tidal.transfer import (
    SkippedTrack,
    SourcePlaylist,
    SourceTrack,
    TrackMetadata,
    TransferError,
    _match_tidal_track,
    _source_track,
    batched,
    choose_playlist_name,
    copy_playlist,
    copy_source_playlist,
    metadata_matches,
    spotify_playlist_id,
)

PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        (f"spotify:playlist:{PLAYLIST_ID}", PLAYLIST_ID),
        (f"https://open.spotify.com/playlist/{PLAYLIST_ID}", PLAYLIST_ID),
        (f"https://open.spotify.com/playlist/{PLAYLIST_ID}?si=abc123", PLAYLIST_ID),
    ],
)
def test_spotify_playlist_id_accepts_playlist_uris(uri: str, expected: str) -> None:
    assert spotify_playlist_id(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "spotify:album:37i9dQZF1DXcBWIGoYBM5M",
        "spotify:playlist:not-an-id",
        f"https://open.spotify.com.evil.example/playlist/{PLAYLIST_ID}",
        f"https://open.spotify.com/track/{PLAYLIST_ID}",
    ],
)
def test_spotify_playlist_id_rejects_other_values(uri: str) -> None:
    with pytest.raises(ValueError, match="Spotify playlist"):
        spotify_playlist_id(uri)


def test_copy_playlist_is_private_unless_public_is_requested() -> None:
    public = inspect.signature(copy_playlist).parameters["public"]

    assert public.default is False
    assert public.kind is inspect.Parameter.KEYWORD_ONLY


def test_choose_playlist_name_keeps_an_unused_name() -> None:
    assert choose_playlist_name("Road Trip", {"Other"}, date(2026, 3, 7)) == "Road Trip"


def test_choose_playlist_name_dates_a_collision() -> None:
    assert choose_playlist_name("Road Trip", {"Road Trip"}, date(2026, 3, 7)) == "260307 Road Trip"


def test_choose_playlist_name_numbers_a_dated_collision() -> None:
    names = {"Road Trip", "260307 Road Trip", "260307 Road Trip (2)"}
    assert choose_playlist_name("Road Trip", names, date(2026, 3, 7)) == "260307 Road Trip (3)"


def test_source_track_reads_current_spotify_playlist_item_shape() -> None:
    playlist_item = {
        "item": {
            "type": "track",
            "is_local": False,
            "uri": "spotify:track:4uLU6hMCjMI75M1A2tKUQC",
            "name": "Never Gonna Give You Up",
            "artists": [{"name": "Rick Astley"}],
            "duration_ms": 213_573,
            "explicit": False,
            "external_ids": {"isrc": "GBARL9300135"},
        }
    }

    track = _source_track(playlist_item, position=1)

    assert isinstance(track, SourceTrack)
    assert track.spotify_uri == "spotify:track:4uLU6hMCjMI75M1A2tKUQC"
    assert track.isrc == "GBARL9300135"


def test_metadata_matches_equivalent_text_and_duration() -> None:
    spotify = TrackMetadata("Le temps de l'amour", ("Françoise Hardy",), 147.2)
    tidal = TrackMetadata("Le Temps de l’Amour", ("Francoise Hardy",), 149.0)

    assert metadata_matches(spotify, tidal)


def test_metadata_match_rejects_a_different_version() -> None:
    spotify = TrackMetadata("Midnight City", ("M83",), 244.0)
    tidal = TrackMetadata("Midnight City (Eric Prydz Private Remix)", ("M83",), 244.0)

    assert not metadata_matches(spotify, tidal)


def test_metadata_match_requires_an_artist_and_close_duration() -> None:
    spotify = TrackMetadata("Home", ("Caribou",), 345.0)

    assert not metadata_matches(spotify, TrackMetadata("Home", ("Four Tet",), 345.0))
    assert not metadata_matches(spotify, TrackMetadata("Home", ("Caribou",), 350.0))


def test_metadata_match_rejects_a_clean_or_explicit_mismatch() -> None:
    spotify = TrackMetadata("Bad Guy", ("Billie Eilish",), 194.1, explicit=False)
    tidal = TrackMetadata("Bad Guy", ("Billie Eilish",), 194.0, explicit=True)

    assert not metadata_matches(spotify, tidal)


def test_batched_preserves_order_and_duplicates() -> None:
    assert list(batched(["1", "1", "2", "3"], size=3)) == [("1", "1", "2"), ("3",)]


def _candidate(
    track_id: int, *, artists=("Lead",), explicit=False, isrc="TEST00000001", duration=60
):
    return SimpleNamespace(
        id=track_id,
        name="Song",
        full_name="Song",
        available=True,
        artists=[SimpleNamespace(name=name) for name in artists],
        explicit=explicit,
        isrc=isrc,
        duration=duration,
    )


def _matching_source(*, artists=("Lead",), explicit=None, isrc=None) -> SourceTrack:
    return SourceTrack(1, "spotify:track:test", isrc, TrackMetadata("Song", artists, 60, explicit))


def test_actual_matcher_rejects_shared_guest_with_wrong_lead() -> None:
    candidate = _candidate(33, artists=("Different lead", "Guest"))
    session = SimpleNamespace(search=lambda *args, **kwargs: {"tracks": [candidate]})

    assert _match_tidal_track(session, _matching_source(artists=("Lead", "Guest"))) is None


def test_actual_matcher_allows_shorter_cross_service_contributor_credits() -> None:
    candidate = _candidate(11)
    session = SimpleNamespace(search=lambda *args, **kwargs: {"tracks": [candidate]})

    assert _match_tidal_track(session, _matching_source(artists=("Lead", "Guest"))) is candidate


@pytest.mark.parametrize("conflict", ["explicit", "isrc", "credits", "artist_id"])
def test_actual_matcher_skips_materially_conflicting_candidates_in_either_order(
    conflict: str,
) -> None:
    first, second = _candidate(11), _candidate(22)
    if conflict == "explicit":
        second.explicit = True
    elif conflict == "isrc":
        second.isrc = "TEST00000002"
    elif conflict == "credits":
        first = _candidate(11, artists=("Lead", "Guest A"))
        second = _candidate(22, artists=("Lead", "Guest B"))
    else:
        first.artists[0].id = 101
        second.artists[0].id = 202
    for candidates in ([first, second], [second, first]):
        session = SimpleNamespace(
            search=lambda *args, tracks=candidates, **kwargs: {"tracks": tracks}
        )
        assert _match_tidal_track(session, _matching_source()) is None


def test_actual_matcher_duplicate_releases_with_compatible_metadata_are_deterministic() -> None:
    first, second = _candidate(11), _candidate(22)
    for candidates in ([first, second], [second, first]):
        session = SimpleNamespace(
            search=lambda *args, tracks=candidates, **kwargs: {"tracks": tracks}
        )
        assert _match_tidal_track(session, _matching_source()) is first


def test_actual_matcher_single_unknown_explicitness_is_not_a_catalog_miss() -> None:
    candidate = _candidate(11, explicit=None)
    session = SimpleNamespace(search=lambda *args, **kwargs: {"tracks": [candidate]})

    assert _match_tidal_track(session, _matching_source()) is candidate


def test_actual_matcher_ignores_unnamed_secondary_credits() -> None:
    candidate = _candidate(11)
    candidate.artists.append(SimpleNamespace(id=202, name=None))
    session = SimpleNamespace(search=lambda *args, **kwargs: {"tracks": [candidate]})

    assert _match_tidal_track(session, _matching_source()) is candidate


def test_actual_matcher_uses_source_explicitness_to_disambiguate() -> None:
    clean, explicit = _candidate(11), _candidate(22, explicit=True)
    session = SimpleNamespace(search=lambda *args, **kwargs: {"tracks": [explicit, clean]})

    assert _match_tidal_track(session, _matching_source(explicit=False)) is clean


def test_actual_matcher_preserves_isrc_first_without_search() -> None:
    exact = _candidate(11, artists=("Other catalog credit",))

    def unexpected_search(*args, **kwargs):
        raise AssertionError("ISRC match must not run metadata search")

    session = SimpleNamespace(get_tracks_by_isrc=lambda isrc: [exact], search=unexpected_search)

    assert _match_tidal_track(session, _matching_source(isrc="TEST00000001")) is exact


@pytest.mark.parametrize("conflict", [None, "explicit", "lead"])
def test_isrc_releases_are_selected_deterministically_only_when_compatible(conflict) -> None:
    first, second = (
        _candidate(11, artists=("Lead", "Guest")),
        _candidate(22, artists=("Lead", "Guest")),
    )
    if conflict == "explicit":
        second.explicit = True
    elif conflict == "lead":
        second.artists.reverse()
    for candidates in ([first, second], [second, first]):
        session = SimpleNamespace(get_tracks_by_isrc=lambda isrc, tracks=candidates: tracks)
        expected = None if conflict else first
        assert _match_tidal_track(session, _matching_source(isrc="TEST00000001")) is expected


def test_isrc_releases_respect_known_source_explicitness_before_selection() -> None:
    clean, explicit = _candidate(11), _candidate(22, explicit=True)
    session = SimpleNamespace(get_tracks_by_isrc=lambda isrc: [explicit, clean])

    assert (
        _match_tidal_track(session, _matching_source(isrc="TEST00000001", explicit=False)) is clean
    )


def test_copy_source_playlist_dry_run_reports_ordered_matches_without_creating(monkeypatch) -> None:
    source_track = SourceTrack(
        position=1,
        spotify_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        isrc=None,
        metadata=TrackMetadata("Song", ("Artist",), 60),
    )
    source = SourcePlaylist("Test", "Description", 1, (source_track,), ())
    monkeypatch.setattr(
        "spotify_playlist_to_tidal.transfer._match_tidal_track",
        lambda session, track: SimpleNamespace(id="tidal-123"),
    )
    tidal = object()

    result = copy_source_playlist(source, tidal, dry_run=True)

    assert result.tidal_uri is None
    assert result.matched == ((1, "tidal-123"),)
    assert result.copied_count == 1


def test_copy_source_playlist_sets_private_before_adding_tracks(monkeypatch) -> None:
    source_track = SourceTrack(
        position=1,
        spotify_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        isrc=None,
        metadata=TrackMetadata("Song", ("Artist",), 60),
    )
    monkeypatch.setattr(
        "spotify_playlist_to_tidal.transfer._match_tidal_track",
        lambda session, track: SimpleNamespace(id="tidal-123"),
    )
    events: list[str] = []
    destination = SimpleNamespace(public=True, num_tracks=0, share_url="tidal:test")

    def make_private() -> bool:
        events.append("private")
        destination.public = False
        return True

    def add_tracks(ids, *, allow_duplicates, limit) -> None:
        events.append("add")
        destination.num_tracks += len(ids)

    destination.set_playlist_private = make_private
    destination.set_playlist_public = lambda: True
    destination.add = add_tracks

    class FakeUser(tidalapi.LoggedInUser):
        pass

    user = object.__new__(FakeUser)
    user.playlists = lambda: ()
    user.create_playlist = lambda name, description: destination

    copy_source_playlist(
        SourcePlaylist("Test", "Description", 1, (source_track,), ()), SimpleNamespace(user=user)
    )

    assert events == ["private", "add"]


def test_copy_source_playlist_refuses_to_create_when_nothing_matches(monkeypatch) -> None:
    source_track = SourceTrack(
        position=1,
        spotify_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        isrc=None,
        metadata=TrackMetadata("Song", ("Artist",), 60),
    )
    monkeypatch.setattr(
        "spotify_playlist_to_tidal.transfer._match_tidal_track",
        lambda session, track: None,
    )

    with pytest.raises(TransferError, match="refusing to create"):
        copy_source_playlist(
            SourcePlaylist("Test", "", 1, (source_track,), (), origin="spotify_web"), object()
        )


def test_copy_source_playlist_dry_run_reports_all_skips_when_nothing_matches(monkeypatch) -> None:
    source_track = SourceTrack(
        position=1,
        spotify_uri="spotify:track:4uLU6hMCjMI75M1A2tKUQC",
        isrc=None,
        metadata=TrackMetadata("Song", ("Artist",), 60),
    )
    monkeypatch.setattr(
        "spotify_playlist_to_tidal.transfer._match_tidal_track",
        lambda session, track: None,
    )

    result = copy_source_playlist(
        SourcePlaylist("Test", "", 1, (source_track,), ()), object(), dry_run=True
    )

    assert result.tidal_uri is None
    assert result.matched == ()
    assert result.copied_count == 0
    assert result.skipped == (SkippedTrack(1, "Artist — Song", "no TIDAL match"),)
