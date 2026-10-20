# ABOUTME: Contains playlist-copying rules and the Spotify-playlist-to-TIDAL workflow.
# ABOUTME: Preserves source order and duplicates while reporting tracks that cannot transfer.

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import combinations, islice
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import spotipy
import tidalapi
from tidalapi.exceptions import InvalidISRC, ObjectNotFound

_SPOTIFY_ID = re.compile(r"[A-Za-z0-9]{22}")
_SPOTIFY_URI = re.compile(r"spotify:playlist:([A-Za-z0-9]{22})")
_NON_WORDS = re.compile(r"[^\w]+")


class TransferError(RuntimeError):
    """Raised when a playlist cannot be copied safely."""


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    title: str
    artists: tuple[str, ...]
    duration_seconds: float
    explicit: bool | None = None


@dataclass(frozen=True, slots=True)
class SourceTrack:
    position: int
    spotify_uri: str
    isrc: str | None
    metadata: TrackMetadata

    @property
    def label(self) -> str:
        return f"{', '.join(self.metadata.artists)} — {self.metadata.title}"


@dataclass(frozen=True, slots=True)
class SkippedTrack:
    position: int
    label: str
    reason: str


@dataclass(frozen=True, slots=True)
class SourcePlaylist:
    name: str
    description: str
    item_count: int
    tracks: tuple[SourceTrack, ...]
    skipped: tuple[SkippedTrack, ...]
    origin: Literal["spotify_api", "spotify_web"] = "spotify_api"


@dataclass(frozen=True, slots=True)
class TransferResult:
    tidal_uri: str | None
    playlist_name: str
    public: bool
    source_count: int
    copied_count: int
    skipped: tuple[SkippedTrack, ...]
    matched: tuple[tuple[int, str], ...] = ()
    dry_run: bool = False


def spotify_playlist_id(uri: str) -> str:
    """Return a playlist ID from a Spotify URI or share URL."""
    uri_match = _SPOTIFY_URI.fullmatch(uri)
    if uri_match:
        return uri_match.group(1)

    parsed = urlparse(uri)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme == "https"
        and parsed.hostname == "open.spotify.com"
        and len(parts) == 2
        and parts[0] == "playlist"
        and _SPOTIFY_ID.fullmatch(parts[1])
    ):
        return parts[1]

    raise ValueError("expected a Spotify playlist URI or URL")


def choose_playlist_name(source_name: str, existing_names: Iterable[str], today: date) -> str:
    """Choose a unique name, adding YYMMDD and then a counter on collisions."""
    names = set(existing_names)
    if source_name not in names:
        return source_name

    dated_name = f"{today:%y%m%d} {source_name}"
    if dated_name not in names:
        return dated_name

    counter = 2
    while f"{dated_name} ({counter})" in names:
        counter += 1
    return f"{dated_name} ({counter})"


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return " ".join(_NON_WORDS.sub(" ", without_marks).split())


def metadata_matches(source: TrackMetadata, candidate: TrackMetadata) -> bool:
    """Conservatively match tracks when an exact ISRC match is unavailable."""
    explicit_matches = (
        source.explicit is None
        or candidate.explicit is None
        or source.explicit == candidate.explicit
    )
    return (
        _normalized(source.title) == _normalized(candidate.title)
        and bool(source.artists and candidate.artists)
        and bool(_normalized(source.artists[0]))
        and _normalized(source.artists[0]) == _normalized(candidate.artists[0])
        and abs(source.duration_seconds - candidate.duration_seconds) <= 3
        and explicit_matches
    )


def batched[T](values: Iterable[T], size: int = 100) -> Iterator[tuple[T, ...]]:
    """Yield fixed-size tuples without changing order or removing duplicates."""
    if size < 1:
        raise ValueError("batch size must be positive")

    iterator = iter(values)
    while batch := tuple(islice(iterator, size)):
        yield batch


def _track_label(track: Mapping[str, Any] | None, position: int) -> str:
    if not track:
        return f"Spotify item {position}"
    artists = ", ".join(artist["name"] for artist in track.get("artists", ()) if artist.get("name"))
    title = track.get("name") or f"Spotify item {position}"
    return f"{artists} — {title}" if artists else title


def _source_track(item: Mapping[str, Any], position: int) -> SourceTrack | SkippedTrack:
    track = item.get("item") or item.get("track")
    label = _track_label(track, position)
    if not track:
        return SkippedTrack(position, label, "unavailable on Spotify")
    if track.get("type") != "track":
        return SkippedTrack(position, label, "not a music track")
    if track.get("is_local"):
        return SkippedTrack(position, label, "local Spotify file")

    artists = tuple(artist["name"] for artist in track.get("artists", ()) if artist.get("name"))
    title = track.get("name")
    duration_ms = track.get("duration_ms")
    spotify_uri = track.get("uri")
    if not title or not artists or not isinstance(duration_ms, (int, float)) or not spotify_uri:
        return SkippedTrack(position, label, "incomplete Spotify metadata")

    external_ids = track.get("external_ids") or {}
    return SourceTrack(
        position=position,
        spotify_uri=spotify_uri,
        isrc=external_ids.get("isrc"),
        metadata=TrackMetadata(
            title,
            artists,
            duration_ms / 1000,
            explicit=track.get("explicit") if isinstance(track.get("explicit"), bool) else None,
        ),
    )


_WEB_MARKDOWN_ESCAPE = re.compile(r"\\([!\\\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
_WEB_TABLE_HEADINGS = frozenset(("#", "Title", "Album", "Date added"))


def _unescape_web_markdown(value: str) -> str:
    """Remove Markdown escapes without treating ordinary backslashes as escapes."""
    return _WEB_MARKDOWN_ESCAPE.sub(r"\1", value)


@dataclass(frozen=True, slots=True)
class _WebLink:
    start: int
    end: int
    label: str
    destination: str


def _closing_web_delimiter(value: str, start: int, opening: str, closing: str) -> int:
    """Scan balanced delimiters, with bounded nesting and Markdown escapes."""
    depth = 1
    index = start + 1
    while index < len(value):
        character = value[index]
        if character == "\\":
            index += 2
            continue
        if character == opening:
            depth += 1
            if depth > 32:
                raise TransferError("unsupported Markdown link nesting in web playlist")
        elif character == closing:
            depth -= 1
            if not depth:
                return index
        index += 1
    raise TransferError("malformed Markdown link in web playlist")


def _web_links(value: str) -> tuple[_WebLink, ...]:
    """Read inline [label](URL) only; this is not a general Markdown parser."""
    links: list[_WebLink] = []
    index = 0
    while index < len(value):
        if value[index] == "\\":
            index += 2
        elif value[index] == "[":
            label_end = _closing_web_delimiter(value, index, "[", "]")
            destination_start = label_end + 1
            if destination_start >= len(value) or value[destination_start] != "(":
                raise TransferError("unsupported Markdown link in web playlist")
            end = _closing_web_delimiter(value, destination_start, "(", ")")
            destination = _unescape_web_markdown(value[destination_start + 1 : end])
            if not destination or any(character.isspace() for character in destination):
                raise TransferError("unsupported Markdown link destination in web playlist")
            links.append(
                _WebLink(
                    index,
                    end + 1,
                    _unescape_web_markdown(value[index + 1 : label_end]),
                    destination,
                )
            )
            index = end + 1
        else:
            index += 1
    return tuple(links)


def _plain_web_markdown(value: str) -> str:
    pieces: list[str] = []
    start = 0
    for link in _web_links(value):
        pieces.extend((_unescape_web_markdown(value[start : link.start]), link.label))
        start = link.end
    pieces.append(_unescape_web_markdown(value[start:]))
    return "".join(pieces)


def _web_resource_id(url: str, resource: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError as error:
        raise TransferError(f"malformed Spotify {resource} link in web playlist") from error
    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "open.spotify.com"
        or len(parts) != 2
        or parts[0] != resource
        or not _SPOTIFY_ID.fullmatch(parts[1])
    ):
        raise TransferError(f"malformed Spotify {resource} link in web playlist")
    return parts[1]


def _web_artists(line: str) -> tuple[str, ...]:
    links = _web_links(line)
    remainder = line
    for link in reversed(links):
        remainder = remainder[: link.start] + remainder[link.end :]
    if not links or remainder.strip(" ,"):
        raise TransferError("malformed track metadata: artists")
    artists = tuple(link.label for link in links)
    if not all(artists):
        raise TransferError("malformed track metadata: artists")
    for link in links:
        _web_resource_id(link.destination, "artist")
    return artists


def _web_duration(value: str) -> float:
    # Bound conversion before int/float: at most six leading digits, and fixed-width
    # subfields. This accepts long recordings without unbounded numeric input.
    if not re.fullmatch(r"[0-9]{1,6}:(?:[0-5][0-9]:)?[0-5][0-9]", value):
        raise TransferError("malformed track metadata: duration")
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = (int(part) for part in parts)
        hours = 0
    else:
        hours, minutes, seconds = (int(part) for part in parts)
    if seconds >= 60 or minutes >= 60 and len(parts) == 3:
        raise TransferError("malformed track metadata: duration")
    duration = hours * 3600 + minutes * 60 + seconds
    if duration <= 0:
        raise TransferError("malformed track metadata: duration")
    return float(duration)


def _web_track(block: list[str], position: int) -> SourceTrack:
    fields = [line.strip() for line in block if line.strip()]
    if len(fields) != 6 or not fields[0].startswith("![]("):
        raise TransferError(f"malformed track metadata at position {position}")

    track_links = _web_links(fields[1])
    if len(track_links) != 1 or (track_links[0].start, track_links[0].end) != (0, len(fields[1])):
        raise TransferError(f"malformed track metadata at position {position}")
    title_match = track_links[0]
    track_id = _web_resource_id(title_match.destination, "track")
    title = title_match.label
    if not title:
        raise TransferError(f"malformed track metadata at position {position}")
    artists = _web_artists(fields[2])

    album_links = _web_links(fields[3])
    if len(album_links) != 1 or (album_links[0].start, album_links[0].end) != (0, len(fields[3])):
        raise TransferError(f"malformed track metadata at position {position}")
    _web_resource_id(album_links[0].destination, "album")
    if not album_links[0].label:
        raise TransferError(f"malformed track metadata at position {position}")

    return SourceTrack(
        position=position,
        spotify_uri=f"spotify:track:{track_id}",
        isrc=None,
        metadata=TrackMetadata(
            title=title, artists=artists, duration_seconds=_web_duration(fields[5])
        ),
    )


def parse_web_playlist(markdown: str, source_uri: str | None = None) -> SourcePlaylist:
    """Parse a complete public Spotify playlist page extracted as Markdown."""
    lines = markdown.splitlines()
    totals = re.findall(r"^(\d+)\s+songs?\b", markdown, flags=re.MULTILINE)
    if len(totals) != 1:
        raise TransferError("web playlist is missing an unambiguous track total")
    total = int(totals[0])

    page_ids: set[str] = set()
    for line in lines:
        if not line.startswith("[Skip to main content]"):
            continue
        identity = _web_links(line)
        if len(identity) != 1 or identity[0].end != len(line):
            raise TransferError("malformed playlist identity in web page")
        try:
            page_ids.add(spotify_playlist_id(identity[0].destination.split("#", 1)[0]))
        except ValueError as error:
            raise TransferError("malformed playlist identity in web page") from error
    if len(page_ids) > 1:
        raise TransferError("conflicting playlist identities in web page")
    if page_ids and source_uri is not None:
        try:
            source_id = spotify_playlist_id(source_uri)
        except ValueError as error:
            raise TransferError("expected a Spotify playlist URI or URL") from error
        if source_id not in page_ids:
            raise TransferError("web page playlist identity does not match source playlist")

    title_index = next(
        (index for index, line in enumerate(lines) if line.startswith("# ") and line[2:].strip()),
        None,
    )
    list_index = next(
        (index for index, line in enumerate(lines) if line.strip() == "List"),
        None,
    )
    if title_index is None or list_index is None or title_index >= list_index:
        raise TransferError("web playlist is missing playlist title or track list")
    name = _plain_web_markdown(lines[title_index][2:].strip())
    if not name:
        raise TransferError("web playlist is missing playlist title")

    header = lines[title_index + 1 : list_index]
    total_index = next(
        (index for index, line in enumerate(header) if re.match(r"^[0-9]+\s+songs?\b", line)),
        None,
    )
    if total_index is None:
        raise TransferError("web playlist is missing header track total")
    # The owner is the final standalone user link in the metadata suffix, not
    # every paragraph containing a user link. Strip only this structural suffix.
    description_end = total_index
    owner_seen = False
    while description_end:
        line = header[description_end - 1].strip()
        if not line or re.fullmatch(r"•[0-9][0-9,\s]* saves?•", line):
            description_end -= 1
        else:
            links = _web_links(line)
            if (
                owner_seen
                or len(links) != 1
                or (links[0].start, links[0].end) != (0, len(line))
                or not links[0].destination.startswith("https://open.spotify.com/user/")
            ):
                break
            owner_seen = True
            description_end -= 1
    description = "\n".join(
        _plain_web_markdown(line.strip()) for line in header[:description_end]
    ).strip()

    index = list_index + 1
    while (
        index < len(lines)
        and not lines[index].strip().isdigit()
        and not lines[index].startswith("Recommended")
    ):
        stripped = lines[index].strip()
        if not stripped or stripped in _WEB_TABLE_HEADINGS:
            index += 1
            continue
        raise TransferError("unexpected content before first track")
    tracks: list[SourceTrack] = []
    expected_position = 1
    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index >= len(lines) or lines[index].startswith("Recommended"):
            break
        if not lines[index].strip().isdigit():
            raise TransferError("malformed or noncontiguous track position")
        position = int(lines[index].strip())
        if position != expected_position:
            raise TransferError("malformed or noncontiguous track position")
        index += 1
        block_start = index
        while (
            index < len(lines)
            and not lines[index].strip().isdigit()
            and not lines[index].startswith("Recommended")
        ):
            index += 1
        tracks.append(_web_track(lines[block_start:index], position))
        expected_position += 1

    if expected_position - 1 != total:
        raise TransferError("web playlist track count does not match declared total")
    return SourcePlaylist(
        name=name,
        description=description,
        item_count=total,
        tracks=tuple(tracks),
        skipped=(),
        origin="spotify_web",
    )


def read_web_playlist(path: str | Path, source_uri: str) -> SourcePlaylist:
    """Read and parse a caller-provided web-extraction file without fetching it."""
    try:
        markdown = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise TransferError(f"could not read web playlist file: {error}") from error
    return parse_web_playlist(markdown, source_uri)


def read_spotify_playlist(session: spotipy.Spotify, playlist_id: str) -> SourcePlaylist:
    """Read playlist metadata and every transferable Spotify track in source order."""
    summary = session.playlist(playlist_id, fields="id,name,description")
    name = summary.get("name")
    if not name:
        raise TransferError("Spotify returned a playlist without a name")

    fields = (
        "items(item(type,is_local,uri,name,artists(name),duration_ms,explicit,"
        "external_ids(isrc))),next"
    )
    page = session.playlist_items(
        playlist_id,
        fields=fields,
        limit=100,
        additional_types=("track", "episode"),
    )
    tracks: list[SourceTrack] = []
    skipped: list[SkippedTrack] = []
    position = 0

    while page:
        for item in page.get("items", ()):
            position += 1
            parsed = _source_track(item, position)
            if isinstance(parsed, SourceTrack):
                tracks.append(parsed)
            else:
                skipped.append(parsed)
        page = session.next(page) if page.get("next") else None

    return SourcePlaylist(
        name=name,
        description=summary.get("description") or "",
        item_count=position,
        tracks=tuple(tracks),
        skipped=tuple(skipped),
    )


def _tidal_metadata(track: tidalapi.Track) -> TrackMetadata:
    title = track.full_name or track.name
    return TrackMetadata(
        title=title,
        artists=tuple(artist.name for artist in (track.artists or ()) if artist.name),
        duration_seconds=float(track.duration),
        explicit=track.explicit,
    )


def _available(tracks: Iterable[tidalapi.Track]) -> list[tidalapi.Track]:
    return [track for track in tracks if track.available]


def _select_compatible_candidate(
    tracks: list[tidalapi.Track], source: TrackMetadata
) -> tidalapi.Track | None:
    """Skip material catalog conflicts; rank only compatible releases deterministically."""
    tracks = [
        track
        for track in tracks
        if source.explicit is None or track.explicit is None or track.explicit == source.explicit
    ]
    if not tracks:
        return None
    if len({track.isrc for track in tracks if track.isrc}) > 1:
        return None
    if len({track.explicit for track in tracks if track.explicit is not None}) > 1:
        return None
    lead_artists = {
        _normalized(track.artists[0].name)
        for track in tracks
        if track.artists and track.artists[0].name
    }
    if len(lead_artists) > 1:
        return None
    credits = [
        {_normalized(artist.name) for artist in track.artists or () if artist.name}
        for track in tracks
    ]
    # Missing secondary credits are common across services; different named guests
    # are not evidence of the same recording, even with a shared lead artist.
    if any(not (left <= right or right <= left) for left, right in combinations(credits, 2)):
        return None
    artist_ids: dict[str, set[str]] = {}
    for track in tracks:
        for artist in track.artists or ():
            artist_id = getattr(artist, "id", None)
            if artist_id is not None and artist.name:
                artist_ids.setdefault(_normalized(artist.name), set()).add(str(artist_id))
    if any(len(ids) > 1 for ids in artist_ids.values()):
        return None
    return min(
        tracks,
        key=lambda track: (abs(track.duration - source.duration_seconds), str(track.id)),
    )


def _match_tidal_track(session: tidalapi.Session, source: SourceTrack) -> tidalapi.Track | None:
    if source.isrc:
        try:
            isrc_matches = _available(session.get_tracks_by_isrc(source.isrc))
        except (InvalidISRC, ObjectNotFound):
            isrc_matches = []
        if isrc_matches:
            return _select_compatible_candidate(isrc_matches, source.metadata)

    query = f"{source.metadata.title} {source.metadata.artists[0]}"
    search_results = session.search(query, models=[tidalapi.Track], limit=20)
    metadata_matches_ = [
        track
        for track in _available(search_results["tracks"])
        if metadata_matches(source.metadata, _tidal_metadata(track))
    ]
    return _select_compatible_candidate(metadata_matches_, source.metadata)


def _set_tidal_visibility(playlist: tidalapi.UserPlaylist, public: bool) -> None:
    if playlist.public is public:
        return

    updated = playlist.set_playlist_public() if public else playlist.set_playlist_private()
    if not updated or playlist.public is not public:
        visibility = "public" if public else "private"
        raise TransferError(
            f"TIDAL could not make the playlist {visibility}; playlist: {playlist.share_url}"
        )


def copy_source_playlist(
    source: SourcePlaylist,
    tidal: tidalapi.Session,
    report: Callable[[str], None] | None = None,
    *,
    public: bool = False,
    dry_run: bool = False,
) -> TransferResult:
    """Match and optionally create a TIDAL playlist from an already-read source."""
    notify = report or (lambda _message: None)
    notify(f'Matching {len(source.tracks)} track(s) from "{source.name}"...')

    tidal_ids: list[str] = []
    matched: list[tuple[int, str]] = []
    match_cache: dict[str, tidalapi.Track | None] = {}
    skipped = list(source.skipped)
    total = len(source.tracks)
    for index, source_track in enumerate(source.tracks, start=1):
        if source_track.spotify_uri not in match_cache:
            match_cache[source_track.spotify_uri] = _match_tidal_track(tidal, source_track)
        match = match_cache[source_track.spotify_uri]
        if match:
            tidal_id = str(match.id)
            tidal_ids.append(tidal_id)
            matched.append((source_track.position, tidal_id))
        else:
            skipped.append(
                SkippedTrack(source_track.position, source_track.label, "no TIDAL match")
            )
        if index == 1 or index % 10 == 0 or index == total:
            notify(f"Checked {index}/{total} track(s)")

    # Preserve API compatibility for empty/all-miss sources. Saved web snapshots
    # deliberately refuse zero-match writes; dry runs report JSON for either origin.
    if source.origin == "spotify_web" and not tidal_ids and not dry_run:
        raise TransferError("no tracks matched TIDAL; refusing to create a playlist")

    ordered_skips = tuple(sorted(skipped, key=lambda track: track.position))
    if dry_run:
        notify(f"Dry run: {len(tidal_ids)}/{source.item_count} track(s) would be copied.")
        return TransferResult(
            tidal_uri=None,
            playlist_name=source.name,
            public=public,
            source_count=source.item_count,
            copied_count=len(tidal_ids),
            skipped=ordered_skips,
            matched=tuple(matched),
            dry_run=True,
        )

    user = tidal.user
    if not isinstance(user, tidalapi.LoggedInUser):
        raise TransferError("TIDAL session is not logged in")
    existing_names = (playlist.name for playlist in user.playlists() if playlist.name)
    destination_name = choose_playlist_name(source.name, existing_names, date.today())
    notify(f'Creating TIDAL playlist "{destination_name}"...')
    destination = user.create_playlist(destination_name, source.description)
    _set_tidal_visibility(destination, False)

    for batch in batched(tidal_ids):
        expected_count = destination.num_tracks + len(batch)
        destination.add(list(batch), allow_duplicates=True, limit=len(batch))
        if destination.num_tracks != expected_count:
            raise TransferError(
                f"TIDAL did not add every track; partial playlist: {destination.share_url}"
            )

    _set_tidal_visibility(destination, public)
    return TransferResult(
        tidal_uri=destination.share_url,
        playlist_name=destination_name,
        public=public,
        source_count=source.item_count,
        copied_count=len(tidal_ids),
        skipped=ordered_skips,
        matched=tuple(matched),
    )


def copy_playlist(
    spotify_uri: str,
    spotify: spotipy.Spotify,
    tidal: tidalapi.Session,
    report: Callable[[str], None] | None = None,
    *,
    public: bool = False,
    dry_run: bool = False,
) -> TransferResult:
    """Copy one Spotify API playlist into a newly created TIDAL playlist."""
    source = read_spotify_playlist(spotify, spotify_playlist_id(spotify_uri))
    return copy_source_playlist(source, tidal, report=report, public=public, dry_run=dry_run)
