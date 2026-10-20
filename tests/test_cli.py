# ABOUTME: Exercises the installed command-line boundary without substituting service clients.
# ABOUTME: Verifies input validation and the machine-readable stdout contract.

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from spotify_playlist_to_tidal.cli import _report_result
from spotify_playlist_to_tidal.transfer import SkippedTrack, TransferResult


def test_dry_run_report_is_json_with_ordered_matches_and_skips(capsys) -> None:
    _report_result(
        TransferResult(
            tidal_uri=None,
            playlist_name="Test",
            public=False,
            source_count=2,
            copied_count=1,
            skipped=(SkippedTrack(2, "Artist — Missing", "no TIDAL match"),),
            matched=((1, "tidal-123"),),
            dry_run=True,
        )
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "dry_run": True,
        "matched": [{"source_position": 1, "tidal_id": "tidal-123"}],
        "matched_count": 1,
        "playlist_name": "Test",
        "public": False,
        "skipped": [
            {
                "label": "Artist — Missing",
                "reason": "no TIDAL match",
                "source_position": 2,
            }
        ],
        "source_count": 2,
    }


def test_help_describes_uri_input_and_output() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "spotify_playlist_to_tidal", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "SPOTIFY_URI" in result.stdout
    assert "TIDAL playlist URI" in result.stdout
    assert "--public" in result.stdout
    assert "--web-file" in result.stdout
    assert "--dry-run" in result.stdout
    assert "private" in result.stdout
    assert result.stderr == ""


def test_auth_help_lists_setup_commands() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "spotify_playlist_to_tidal", "auth", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    login_help = subprocess.run(
        [sys.executable, "-m", "spotify_playlist_to_tidal", "auth", "login", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "login" in result.stdout
    assert "status" in result.stdout
    assert "path" in result.stdout
    assert result.stderr == ""
    assert login_help.returncode == 0
    assert "--manual" in login_help.stdout
    assert login_help.stderr == ""


def test_auth_path_needs_no_credentials(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "spotify_playlist_to_tidal", "auth", "path"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    state_path = Path(result.stdout.strip())
    assert result.returncode == 0
    assert state_path.name == "spotify-playlist-to-tidal"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o700
    assert result.stderr == ""


def test_auth_status_is_noninteractive_when_setup_is_missing(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path)
    for name in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        environment.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-m", "spotify_playlist_to_tidal", "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "spotify: not configured",
        "tidal: not authenticated",
    ]
    assert result.stderr == ""


def test_invalid_uri_fails_without_stdout() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "spotify_playlist_to_tidal", "spotify:album:not-a-playlist"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "expected a Spotify playlist URI" in result.stderr


def test_transfer_does_not_start_interactive_login_when_tokens_are_missing(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment["HOME"] = str(tmp_path)
    environment["SPOTIPY_CLIENT_ID"] = "configured-client"
    environment["SPOTIPY_CLIENT_SECRET"] = "configured-secret"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spotify_playlist_to_tidal",
            "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "Spotify authentication required" in result.stderr
    assert "auth login" in result.stderr


def test_invalid_web_file_fails_before_spotify_auth(tmp_path: Path) -> None:
    web_file = tmp_path / "malformed-test-only.md"
    web_file.write_text("not a Spotify page", encoding="utf-8")
    environment = os.environ.copy()
    for name in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        environment.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spotify_playlist_to_tidal",
            "--web-file",
            str(web_file),
            "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "track total" in result.stderr
    assert "SPOTIPY_CLIENT_ID" not in result.stderr


def test_missing_spotify_credentials_fail_without_stdout() -> None:
    environment = os.environ.copy()
    for name in ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET", "SPOTIPY_REDIRECT_URI"):
        environment.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spotify_playlist_to_tidal",
            "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "SPOTIPY_CLIENT_ID" in result.stderr
    assert "SPOTIPY_CLIENT_SECRET" in result.stderr
