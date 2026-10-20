# ABOUTME: Exercises transfer and CLI flows through installed tidalapi on a local transport.
# ABOUTME: Uses synthetic source/catalog responses to assert compatibility and write ordering.

import json

import pytest
from conftest import catalog_track, spotify_input

from spotify_playlist_to_tidal import cli
from spotify_playlist_to_tidal.transfer import copy_playlist

URL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"


@pytest.mark.parametrize("count", [0, 1])
def test_api_copy_creates_empty_destination_for_empty_or_all_missed_source(local_tidal, count):
    result = copy_playlist(URL, spotify_input(count), local_tidal.session)

    assert result.tidal_uri == "https://tidal.com/browse/playlist/1"
    assert result.source_count == count
    assert result.copied_count == 0
    assert len(result.skipped) == count
    assert local_tidal.items == {"1": []}
    assert local_tidal.documents["1"]["description"] == "Description"
    assert local_tidal.documents["1"]["publicPlaylist"] is False
    assert not any(event["method"] == "POST" for event in local_tidal.events)


def _web_file(tmp_path, count):
    path = tmp_path / "synthetic-public-page.md"
    row = (
        (
            "1\n![](https://i.scdn.co/image/test)\n"
            "[Song](https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC)\n"
            "[Lead](https://open.spotify.com/artist/4jEa9eTpzzkuDQ9JMr0LT3)\n"
            "[Album](https://open.spotify.com/album/5OCJBzbEAE30YLojP5Pgo9)\nJan 1, 2026\n1:00\n"
        )
        if count
        else ""
    )
    path.write_text(
        f"# Test\nDescription\n[Owner](https://open.spotify.com/user/test)\n{count} songs\nList\n"
        + row
        + "RecommendedBased on this playlist\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("web", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("count", [0, 1])
def test_cli_zero_matches_have_source_specific_write_semantics(
    local_tidal, monkeypatch, tmp_path, capsys, web, dry_run, count
):
    monkeypatch.setattr(cli, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(cli, "tidal_session", lambda path: local_tidal.session)

    def unexpected_spotify(*args):
        raise AssertionError("web input must not require Spotify setup")

    monkeypatch.setattr(
        cli, "validate_spotify_configuration", unexpected_spotify if web else lambda: None
    )
    monkeypatch.setattr(
        cli, "spotify_session", unexpected_spotify if web else lambda path: spotify_input(count)
    )
    arguments = ["--web-file", str(_web_file(tmp_path, count))] if web else []
    if dry_run:
        arguments.append("--dry-run")
    arguments.append(URL)

    status = cli.run(arguments)

    output = capsys.readouterr()
    if dry_run:
        assert status == 0, output.err
        payload = json.loads(output.out)
        assert payload["dry_run"] is True
        assert payload["source_count"] == count
        assert payload["matched_count"] == 0
        assert payload["matched"] == []
        assert len(payload["skipped"]) == count
        assert len(output.out.splitlines()) == 1
        assert not local_tidal.documents
    elif web:
        assert status == 1
        assert output.out == ""
        assert "no tracks matched TIDAL; refusing to create" in output.err
        assert not local_tidal.documents
    else:
        assert status == 0, output.err
        assert output.out == "https://tidal.com/browse/playlist/1\n"
        assert local_tidal.items == {"1": []}


@pytest.mark.parametrize("web", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_cli_success_uses_real_matcher_and_library_with_clean_stdout(
    local_tidal, monkeypatch, tmp_path, capsys, web, dry_run
):
    local_tidal.catalog.append(catalog_track())
    monkeypatch.setattr(cli, "state_directory", lambda: tmp_path)
    monkeypatch.setattr(cli, "tidal_session", lambda path: local_tidal.session)
    monkeypatch.setattr(cli, "validate_spotify_configuration", lambda: None)
    monkeypatch.setattr(cli, "spotify_session", lambda path: spotify_input(1))
    arguments = ["--web-file", str(_web_file(tmp_path, 1))] if web else []
    if dry_run:
        arguments.append("--dry-run")

    assert cli.run([*arguments, URL]) == 0

    output = capsys.readouterr()
    assert "Matching 1 track(s)" in output.err
    if dry_run:
        assert json.loads(output.out)["matched"] == [{"source_position": 1, "tidal_id": "11"}]
        assert all(event["path"] == "search" for event in local_tidal.events)
        assert not local_tidal.documents
    else:
        assert output.out == "https://tidal.com/browse/playlist/1\n"
        assert local_tidal.items == {"1": ["11"]}
        assert local_tidal.documents["1"]["publicPlaylist"] is False


def test_real_library_multibatch_preserves_order_duplicates_and_privacy(local_tidal):
    first, second = catalog_track(11), catalog_track(22)
    second["title"] = "Other"
    local_tidal.catalog.extend((first, second))
    spotify = spotify_input(202)
    first_source = spotify.playlist_items()["items"][0]
    second_source = {
        "item": {**first_source["item"], "name": "Other", "uri": "spotify:track:other"}
    }
    spotify.playlist_items = lambda *args, **kwargs: {
        "items": [first_source, second_source] * 101,
        "next": None,
    }

    result = copy_playlist(URL, spotify, local_tidal.session, public=True)

    assert result.copied_count == 202
    assert local_tidal.items["1"] == ["11", "22"] * 101
    adds = [event for event in local_tidal.events if event["method"] == "POST"]
    assert [event["data"]["toIndex"] for event in adds] == [0, 100, 200]
    assert [event["params"]["limit"] for event in adds] == [100, 100, 2]
    assert all(event["data"]["onDupes"] == "ADD" for event in adds)
    writes = [event["path"] for event in local_tidal.events if event["method"] != "GET"]
    assert writes[0].endswith("/create-playlist")
    assert writes[1].endswith("/set-private")
    assert writes[-1].endswith("/set-public")
    assert local_tidal.documents["1"]["publicPlaylist"] is True
    assert len([event for event in local_tidal.events if event["path"] == "search"]) == 2
