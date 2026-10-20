# ABOUTME: Tests strict parsing of public Spotify playlist web-extraction markdown.
# ABOUTME: Keeps fixtures miniature and synthetic while covering source identity and completeness.

from pathlib import Path

import pytest

from spotify_playlist_to_tidal.transfer import SourcePlaylist, TransferError, parse_web_playlist

PLAYLIST_URL = "https://open.spotify.com/playlist/1tHNL2eat97A0b4ZxDcEQp"
TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
SECOND_TRACK_ID = "6WrqLxFkIlHcBT8DmnYmRL"


def _row(position: int, track_id: str, title: str, artist: str, duration: str) -> str:
    return f"""{position}

![](https://i.scdn.co/image/test)

[{title}](https://open.spotify.com/track/{track_id})
[{artist}](https://open.spotify.com/artist/4jEa9eTpzzkuDQ9JMr0LT3)

[Album {position}](https://open.spotify.com/album/5OCJBzbEAE30YLojP5Pgo9)

Jan 1, 2026

{duration}
"""


def _page(*rows: str, total: int | None = None, playlist_url: str = PLAYLIST_URL) -> str:
    count = len(rows) if total is None else total
    return "\n".join(
        [
            f"[Skip to main content]({playlist_url}#main-view)",
            "",
            "# Test Playlist",
            "A description with an escaped \\| pipe.",
            "[Owner](https://open.spotify.com/user/test)",
            "",
            f"{count} songs, about 1 min",
            "",
            "List",
            "",
            *rows,
            "RecommendedBased on what's in this playlist",
            "",
            "[Not a playlist track](https://open.spotify.com/track/7t5JE2lEgT3tuMFhAM4l58)",
        ]
    )


def test_parse_web_playlist_preserves_order_duplicates_and_unescapes_markdown() -> None:
    markdown = _page(
        _row(1, TRACK_ID, r"A \*star\* \ path", "Artist One", "1:02"),
        _row(2, TRACK_ID, r"A \*star\* \ path", "Artist One", "1:02"),
    )

    playlist = parse_web_playlist(markdown, PLAYLIST_URL)

    assert isinstance(playlist, SourcePlaylist)
    assert playlist.name == "Test Playlist"
    assert playlist.description == "A description with an escaped | pipe."
    assert playlist.item_count == 2
    assert [track.position for track in playlist.tracks] == [1, 2]
    assert [track.spotify_uri for track in playlist.tracks] == [
        f"spotify:track:{TRACK_ID}",
        f"spotify:track:{TRACK_ID}",
    ]
    assert playlist.tracks[0].metadata.title == r"A *star* \ path"
    assert playlist.tracks[0].metadata.artists == ("Artist One",)
    assert playlist.tracks[0].metadata.duration_seconds == 62
    assert playlist.skipped == ()


def test_parse_web_playlist_stops_before_recommendations() -> None:
    playlist = parse_web_playlist(_page(_row(1, TRACK_ID, "Song", "Artist", "0:30")), PLAYLIST_URL)

    assert len(playlist.tracks) == 1
    assert all("Not a playlist track" not in track.metadata.title for track in playlist.tracks)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda page: page.replace("2 songs", "", 1), "total"),
        (lambda page: page.replace("1\n\n![]", "2\n\n![]", 1), "position"),
        (lambda page: page.replace("1:02", "not-a-duration", 1), "metadata"),
        (lambda page: page.replace(TRACK_ID, "bad-id", 1), "track"),
    ],
)
def test_parse_web_playlist_rejects_incomplete_or_malformed_input(mutator, message: str) -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", "1:02"), total=2)

    with pytest.raises(TransferError, match=message):
        parse_web_playlist(mutator(page), PLAYLIST_URL)


def test_parse_web_playlist_rejects_empty_track_title() -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", "1:02")).replace(
        "[Song](https://open.spotify.com/track/", "[](https://open.spotify.com/track/", 1
    )

    with pytest.raises(TransferError, match="metadata"):
        parse_web_playlist(page, PLAYLIST_URL)


def test_parse_web_playlist_rejects_empty_album_label() -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", "1:02")).replace(
        "[Album 1](https://open.spotify.com/album/", "[](https://open.spotify.com/album/", 1
    )

    with pytest.raises(TransferError, match="metadata"):
        parse_web_playlist(page, PLAYLIST_URL)


def test_parse_web_playlist_rejects_unexpected_content_before_first_row() -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", "1:02")).replace(
        "\nList\n\n", "\nList\nUnexpected junk\n\n", 1
    )

    with pytest.raises(TransferError, match="before first track"):
        parse_web_playlist(page, PLAYLIST_URL)


def test_parse_web_playlist_excludes_singular_save_chrome_from_description() -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", "1:02")).replace(
        "A description with an escaped \\| pipe.\n[Owner]",
        "A description with an escaped \\| pipe.\n•1 save•\n[Owner]",
        1,
    )

    playlist = parse_web_playlist(page, PLAYLIST_URL)

    assert playlist.description == "A description with an escaped | pipe."


def test_parse_web_playlist_converts_malformed_urlparse_links_to_transfer_error() -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", "1:02")).replace(
        "https://open.spotify.com/album/5OCJBzbEAE30YLojP5Pgo9", "https://[", 1
    )

    with pytest.raises(TransferError, match="malformed Spotify album"):
        parse_web_playlist(page, PLAYLIST_URL)


def test_parse_web_playlist_rejects_noncontiguous_positions() -> None:
    page = _page(
        _row(1, TRACK_ID, "Song", "Artist", "1:02"),
        _row(3, SECOND_TRACK_ID, "Other", "Artist", "1:03"),
    )

    with pytest.raises(TransferError, match="position"):
        parse_web_playlist(page, PLAYLIST_URL)


def test_parse_web_playlist_rejects_page_identity_mismatch() -> None:
    page = _page(
        _row(1, TRACK_ID, "Song", "Artist", "1:02"),
        playlist_url="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
    )

    with pytest.raises(TransferError, match="playlist identity"):
        parse_web_playlist(page, PLAYLIST_URL)


def test_read_web_playlist_uses_a_local_file_without_fetching(tmp_path: Path) -> None:
    from spotify_playlist_to_tidal.transfer import read_web_playlist

    path = tmp_path / "test-only-public-playlist.md"
    path.write_text(_page(_row(1, TRACK_ID, "Song", "Artist", "1:02")), encoding="utf-8")

    playlist = read_web_playlist(path, PLAYLIST_URL)

    assert playlist.name == "Test Playlist"
    assert playlist.tracks[0].position == 1


@pytest.mark.parametrize("duration", ["3:4", "1:2:03", "3:²", "3:４５", "9" * 400 + ":00"])
def test_web_duration_rejects_truncation_non_ascii_and_unbounded_values(duration: str) -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", duration))
    # The final field may be truncated at EOF, without a recommendations footer.
    page = page.split("Recommended", 1)[0].rstrip()

    with pytest.raises(TransferError, match="duration"):
        parse_web_playlist(page, PLAYLIST_URL)


@pytest.mark.parametrize(("duration", "seconds"), [("123:45", 7425), ("1:02:03", 3723)])
def test_web_duration_accepts_complete_minutes_and_hours(duration: str, seconds: int) -> None:
    playlist = parse_web_playlist(_page(_row(1, TRACK_ID, "Song", "Artist", duration)))

    assert playlist.tracks[0].metadata.duration_seconds == seconds


def test_description_preserves_user_links_and_paragraphs_but_not_header_chrome() -> None:
    page = (
        _page(_row(1, TRACK_ID, "Song", "Artist", "1:02"))
        .replace(
            "A description with an escaped \\| pipe.",
            "Curated by [Guest](https://open.spotify.com/user/guest). Updated weekly.\n\n"
            "[Collaborator](https://open.spotify.com/user/collaborator)\n\nLast paragraph.",
        )
        .replace("\n1 songs", "\n•1,234 saves•\n\n1 songs")
    )

    playlist = parse_web_playlist(page, PLAYLIST_URL)

    assert playlist.description == (
        "Curated by Guest. Updated weekly.\n\nCollaborator\n\nLast paragraph."
    )


@pytest.mark.parametrize(
    "destination", ["https://example.com/mix_(live)", r"https://example.com/mix_\(live\)"]
)
def test_balanced_inline_links_preserve_visible_labels_everywhere(destination: str) -> None:
    page = _page(_row(1, TRACK_ID, r"Song \[Live\]", r"Artist \[A\]", "1:02"))
    page = page.replace("# Test Playlist", rf"# [Mix \[Live\]]({destination})")
    page = page.replace(
        "A description with an escaped \\| pipe.",
        rf"Listen to [my \[live\] mix]({destination}) today. Literal \[brackets\].",
    ).replace("[Album 1]", r"[Album \[One\]]")
    page = page.replace("[Owner]", r"[Owner \[O\]]")

    playlist = parse_web_playlist(page, PLAYLIST_URL)

    assert playlist.name == "Mix [Live]"
    assert playlist.description == "Listen to my [live] mix today. Literal [brackets]."
    assert playlist.tracks[0].metadata.title == "Song [Live]"
    assert playlist.tracks[0].metadata.artists == ("Artist [A]",)


@pytest.mark.parametrize(
    "description",
    [
        "Listen to [mix](https://example.com/mix_(live)",
        r"Listen to [mix\](https://example.com/mix)",
        "Listen to [mix]()",
    ],
)
def test_malformed_inline_links_fail_without_partial_substitution(description: str) -> None:
    page = _page(_row(1, TRACK_ID, "Song", "Artist", "1:02")).replace(
        "A description with an escaped \\| pipe.", description
    )

    with pytest.raises(TransferError, match="Markdown"):
        parse_web_playlist(page, PLAYLIST_URL)


def test_conflicting_page_identities_are_rejected_even_if_first_matches() -> None:
    page = _page(
        _row(1, TRACK_ID, "Song", "Artist", "1:02"),
        playlist_url=("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"),
    )
    page = f"[Skip to main content]({PLAYLIST_URL}#main-view)\n" + page

    with pytest.raises(TransferError, match="playlist identit"):
        parse_web_playlist(page, PLAYLIST_URL)
