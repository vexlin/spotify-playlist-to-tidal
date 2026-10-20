# ABOUTME: Defines the one-input, one-output command-line interface for playlist copying.
# ABOUTME: Sends human-readable status to stderr and only the new TIDAL URI to stdout.

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import NoReturn

from spotipy.exceptions import SpotifyBaseException
from tidalapi.exceptions import TidalAPIError

from .auth import (
    AuthenticationError,
    ConfigurationError,
    login_spotify,
    login_tidal,
    spotify_session,
    state_directory,
    tidal_session,
    validate_spotify_configuration,
)
from .transfer import (
    TransferError,
    TransferResult,
    copy_playlist,
    copy_source_playlist,
    read_web_playlist,
    spotify_playlist_id,
)

_COMMAND = "spotify-playlist-to-tidal"


def _transfer_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_COMMAND,
        description="Copy one Spotify playlist to a newly created TIDAL playlist.",
        epilog=(
            "On success, stdout contains only the new TIDAL playlist URI. "
            f"For authentication setup, run: {_COMMAND} auth --help"
        ),
    )
    parser.add_argument(
        "spotify_uri",
        metavar="SPOTIFY_URI",
        help="spotify:playlist:... URI or https://open.spotify.com/playlist/... URL",
    )
    parser.add_argument(
        "--web-file",
        metavar="PATH",
        help="use a locally saved public Spotify web-extraction Markdown file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="match tracks and report the planned copy without creating a TIDAL playlist",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="publish the new TIDAL playlist publicly (default: private)",
    )
    return parser


def _auth_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_COMMAND} auth",
        description="Set up and inspect reusable Spotify and TIDAL authentication.",
    )
    commands = parser.add_subparsers(dest="auth_command", required=True)
    login = commands.add_parser("login", help="authenticate both services")
    login.add_argument(
        "--manual",
        action="store_true",
        help="print authorization links instead of opening a local browser",
    )
    commands.add_parser("status", help="validate cached authentication without login")
    commands.add_parser("path", help="print the directory containing OAuth tokens")
    return parser


def _report(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _report_result(result: TransferResult) -> None:
    if result.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "playlist_name": result.playlist_name,
                    "public": result.public,
                    "source_count": result.source_count,
                    "matched_count": result.copied_count,
                    "matched": [
                        {"source_position": position, "tidal_id": tidal_id}
                        for position, tidal_id in result.matched
                    ],
                    "skipped": [
                        {
                            "source_position": skipped.position,
                            "label": skipped.label,
                            "reason": skipped.reason,
                        }
                        for skipped in result.skipped
                    ],
                },
                sort_keys=True,
            )
        )
        return
    if result.skipped:
        _report(f"Skipped {len(result.skipped)} track(s):")
        for skipped in result.skipped:
            label = " ".join(skipped.label.splitlines())
            _report(f"  {skipped.position}. {label} ({skipped.reason})")
    visibility = "public" if result.public else "private"
    _report(
        f'Created "{result.playlist_name}" as a {visibility} playlist with '
        f"{result.copied_count}/{result.source_count} track(s)."
    )


def _authentication_status() -> int:
    state_path = state_directory()
    authenticated = True

    try:
        validate_spotify_configuration()
        spotify_session(state_path).current_user()
    except ConfigurationError:
        spotify_status = "not configured"
        authenticated = False
    except AuthenticationError:
        spotify_status = "not authenticated"
        authenticated = False
    except (SpotifyBaseException, OSError) as error:
        spotify_status = f"error ({error})"
        authenticated = False
    else:
        spotify_status = "authenticated"

    try:
        tidal_session(state_path)
    except AuthenticationError:
        tidal_status = "not authenticated"
        authenticated = False
    except (TidalAPIError, OSError) as error:
        tidal_status = f"error ({error})"
        authenticated = False
    else:
        tidal_status = "authenticated"

    print(f"spotify: {spotify_status}")
    print(f"tidal: {tidal_status}")
    return 0 if authenticated else 1


def _run_auth(arguments: Sequence[str]) -> int:
    args = _auth_parser().parse_args(arguments)
    if args.auth_command == "path":
        print(state_directory())
        return 0
    if args.auth_command == "status":
        return _authentication_status()

    try:
        validate_spotify_configuration()
        state_path = state_directory()
        login_spotify(state_path, manual=args.manual, report=_report)
        login_tidal(state_path, manual=args.manual, report=_report)
    except (
        AuthenticationError,
        ConfigurationError,
        SpotifyBaseException,
        TidalAPIError,
        OSError,
    ) as error:
        _report(f"error: {error}")
        return 1
    return _authentication_status()


def _run_transfer(arguments: Sequence[str]) -> int:
    parser = _transfer_parser()
    args = parser.parse_args(arguments)
    try:
        spotify_playlist_id(args.spotify_uri)
    except ValueError as error:
        parser.error(str(error))

    try:
        if args.web_file:
            source = read_web_playlist(args.web_file, args.spotify_uri)
            tidal = tidal_session(state_directory())
            result = copy_source_playlist(
                source,
                tidal,
                report=_report,
                public=args.public,
                dry_run=args.dry_run,
            )
        else:
            validate_spotify_configuration()
            state_path = state_directory()
            spotify = spotify_session(state_path)
            tidal = tidal_session(state_path)
            result = copy_playlist(
                args.spotify_uri,
                spotify,
                tidal,
                report=_report,
                public=args.public,
                dry_run=args.dry_run,
            )
    except (
        AuthenticationError,
        ConfigurationError,
        TransferError,
        SpotifyBaseException,
        TidalAPIError,
        OSError,
    ) as error:
        _report(f"error: {error}")
        return 1

    _report_result(result)
    if not result.dry_run:
        print(result.tidal_uri, flush=True)
    return 0


def run(arguments: Sequence[str] | None = None) -> int:
    """Dispatch authentication commands or one-URI playlist transfers."""
    command_arguments = list(arguments) if arguments is not None else sys.argv[1:]
    if command_arguments[:1] == ["auth"]:
        return _run_auth(command_arguments[1:])
    return _run_transfer(command_arguments)


def main() -> NoReturn:
    """Console-script entry point."""
    raise SystemExit(run())
