from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from pypresence import ActivityType, AioPresence
from pypresence.exceptions import DiscordError, InvalidID, PipeClosed, ServerError
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSession,
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions


APP_CLIENT_ID = "1502442948527128617"
ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_CONFIG_PATH = ROOT / "config.example.json"
COVER_CACHE_DIR = ROOT / "cache" / "covers"
MAX_COVER_BYTES = 8 * 1024 * 1024


DEFAULT_CONFIG: dict[str, Any] = {
    "discord_client_id": APP_CLIENT_ID,
    "poll_interval_seconds": 3,
    "source_filters": ["yandex", "music", "яндекс", "музыка"],
    "show_when_paused": True,
    "activity_type": "listening",
    "timestamp_mode": "both",
    "seek_update_threshold_seconds": 3,
    "loading_grace_seconds": 45,
    "pause": {
        "show_elapsed": True,
        "update_interval_seconds": 5,
        "state_template": "На паузе {pause_elapsed} • {progress}",
        "small_text_template": "На паузе {pause_elapsed}",
    },
    "status_template": {"details": "{title}", "state": "{artist}"},
    "progress_bar": {
        "enabled": False,
        "length": 12,
        "filled_char": "━",
        "empty_char": "─",
        "cursor_char": "●",
        "show_time": True,
    },
    "track_button": {"enabled": True, "label": "Открыть трек"},
    "cover_art": {
        "enabled": True,
        "search_yandex_public_cover": True,
        "host": "127.0.0.1",
        "port": 17654,
        "public_base_url": "",
        "fallback_large_image": "",
    },
    "fallback_text": {"unknown_title": "Unknown track", "unknown_artist": "Unknown artist"},
    "assets": {
        "large_text": "Yandex Music",
        "small_image_playing": "",
        "small_image_paused": "",
        "small_text_playing": "Playing",
        "small_text_paused": "Paused",
    },
    "buttons": [{"label": "Yandex Music", "url": "https://music.yandex.ru/"}],
}


@dataclass(frozen=True)
class Track:
    source: str
    title: str
    artist: str
    album: str
    is_playing: bool
    position_seconds: float | None
    duration_seconds: float | None
    cover_url: str | None
    track_url: str | None

    @property
    def signature(self) -> tuple[Any, ...]:
        return (
            self.source,
            self.title,
            self.artist,
            self.album,
            self.is_playing,
            int(self.duration_seconds or 0),
            self.cover_url,
            self.track_url,
        )


@dataclass(frozen=True)
class YandexTrackInfo:
    cover_url: str | None
    track_url: str | None


@dataclass
class PauseState:
    track_signature: tuple[Any, ...]
    started_at: float
    position_seconds: float | None

    def elapsed_seconds(self) -> float:
        return max(0, time.time() - self.started_at)


class CoverServer:
    def __init__(self, config: dict[str, Any]):
        cover_config = config.get("cover_art", {})
        if not isinstance(cover_config, dict):
            cover_config = {}

        self.enabled = bool(cover_config.get("enabled", True))
        self.host = str(cover_config.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        self.port = int(cover_config.get("port", 17654))
        self.public_base_url = str(cover_config.get("public_base_url", "")).strip().rstrip("/")
        self.cache_dir = COVER_CACHE_DIR
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        if not self.enabled:
            return

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        handler = self._handler_factory(self.cache_dir)
        try:
            self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            logging.warning("Cover server port %s is unavailable: %s. Trying a random port.", self.port, exc)
            self.httpd = ThreadingHTTPServer((self.host, 0), handler)

        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="cover-server", daemon=True)
        self.thread.start()
        logging.info("Cover server: %s/cover/<file>", self.base_url)

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def save(self, image_bytes: bytes | None) -> str | None:
        if not self.enabled or not image_bytes:
            return None

        image_type = detect_image_type(image_bytes)
        if image_type is None:
            logging.debug("Windows returned a thumbnail in an unsupported image format.")
            return None

        extension, _mime = image_type
        digest = hashlib.sha256(image_bytes).hexdigest()[:32]
        filename = f"{digest}{extension}"
        target = self.cache_dir / filename

        if not target.exists():
            target.write_bytes(image_bytes)

        return f"{self.base_url}/cover/{filename}"

    @staticmethod
    def _handler_factory(cache_dir: Path) -> type[BaseHTTPRequestHandler]:
        class CoverRequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self.send_response(HTTPStatus.OK)
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return

                if not parsed.path.startswith("/cover/"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                filename = Path(unquote(parsed.path.removeprefix("/cover/"))).name
                file_path = cache_dir / filename
                if not file_path.exists() or not file_path.is_file():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                mime_type = mime_type_for_path(file_path)
                data = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt: str, *args: Any) -> None:
                logging.debug("Cover server: " + fmt, *args)

        return CoverRequestHandler


class DiscordPresenceClient:
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.rpc: AioPresence | None = None
        self.connected = False

    async def connect(self) -> bool:
        if self.connected:
            return True

        try:
            self.rpc = AioPresence(self.client_id)
            await self.rpc.connect()
            self.connected = True
            logging.info("Connected to Discord RPC.")
            return True
        except (DiscordError, FileNotFoundError, InvalidID, PipeClosed, ConnectionRefusedError, OSError) as exc:
            self.rpc = None
            self.connected = False
            logging.warning("Discord RPC is unavailable: %s", exc)
            return False

    async def clear(self) -> None:
        if not await self.connect() or self.rpc is None:
            return

        try:
            await self.rpc.clear()
        except (DiscordError, ServerError, PipeClosed, BrokenPipeError, OSError) as exc:
            self.connected = False
            logging.warning("Failed to clear Discord activity: %s", exc)

    async def update(self, payload: dict[str, Any]) -> bool:
        if not await self.connect() or self.rpc is None:
            return False

        try:
            await self.rpc.update(**payload)
            return True
        except ServerError as exc:
            if payload.get("large_image"):
                fallback_payload = dict(payload)
                fallback_payload.pop("large_image", None)
                fallback_payload.pop("large_text", None)
                logging.warning("Discord rejected activity image: %s. Retrying without cover.", exc)
                try:
                    await self.rpc.update(**fallback_payload)
                    return True
                except (DiscordError, ServerError, PipeClosed, BrokenPipeError, OSError) as retry_exc:
                    self.connected = False
                    logging.warning("Failed to update Discord activity after image retry: %s", retry_exc)
                    return False

            self.connected = False
            logging.warning("Failed to update Discord activity: %s", exc)
            return False
        except (DiscordError, PipeClosed, BrokenPipeError, OSError) as exc:
            self.connected = False
            logging.warning("Failed to update Discord activity: %s", exc)
            return False


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            user_config = json.load(config_file)
    else:
        user_config = {}
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("Created config.json with default settings.")

    config = deep_merge(DEFAULT_CONFIG, user_config)
    client_id = str(config.get("discord_client_id", "")).strip()
    if not client_id or client_id == "PUT_DISCORD_APPLICATION_ID_HERE":
        client_id = APP_CLIENT_ID
    if not client_id:
        raise SystemExit("Set discord_client_id in config.json first.")

    config["discord_client_id"] = client_id
    return config


def get_nested(config: dict[str, Any], key: str, default: Any = None) -> Any:
    current: Any = config
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def timespan_to_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    try:
        return float(value) / 10_000_000
    except (TypeError, ValueError):
        return None


def timeline_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def current_timeline_position_seconds(
    base_position_seconds: float | None,
    last_updated_time: Any,
    is_playing: bool,
) -> float | None:
    if base_position_seconds is None:
        return None
    if not is_playing:
        return base_position_seconds

    updated_at = timeline_datetime(last_updated_time)
    if updated_at is None:
        return base_position_seconds

    elapsed = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if elapsed < 0:
        return base_position_seconds

    return base_position_seconds + elapsed


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if data.startswith(b"BM"):
        return ".bmp", "image/bmp"
    return None


def mime_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".jpg":
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".gif":
        return "image/gif"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".bmp":
        return "image/bmp"
    return "application/octet-stream"


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def cover_uri_to_url(cover_uri: str) -> str | None:
    cover_uri = cover_uri.strip()
    if not cover_uri:
        return None
    if cover_uri.startswith("//"):
        cover_uri = "https:" + cover_uri
    elif not cover_uri.startswith(("http://", "https://")):
        cover_uri = "https://" + cover_uri
    return cover_uri.replace("%%", "1000x1000")


def yandex_search_url(title: str, artist: str, album: str) -> str:
    query = " ".join(part for part in (title, artist, album) if part).strip()
    return f"https://music.yandex.ru/search?text={quote_plus(query)}"


def extract_cover_uri(track: dict[str, Any]) -> str | None:
    for key in ("coverUri", "ogImage"):
        value = track.get(key)
        if isinstance(value, str) and value.strip():
            return value

    albums = track.get("albums")
    if isinstance(albums, list):
        for album in albums:
            if not isinstance(album, dict):
                continue
            for key in ("coverUri", "ogImage"):
                value = album.get(key)
                if isinstance(value, str) and value.strip():
                    return value

    return None


def extract_track_url(track: dict[str, Any], title: str, artist: str, album: str) -> str | None:
    track_id = str(track.get("realId") or track.get("id") or "").strip()
    album_id = ""

    albums = track.get("albums")
    if isinstance(albums, list):
        for album_item in albums:
            if isinstance(album_item, dict) and album_item.get("id"):
                album_id = str(album_item["id"]).strip()
                break

    if track_id and album_id:
        return f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
    if track_id:
        return f"https://music.yandex.ru/track/{track_id}"

    return yandex_search_url(title, artist, album)


def yandex_track_score(track: dict[str, Any], title: str, artist: str) -> int:
    score = 0
    wanted_title = normalize_text(title)
    wanted_artist = normalize_text(artist)
    found_title = normalize_text(str(track.get("title", "")))

    if wanted_title and found_title == wanted_title:
        score += 5
    elif wanted_title and (wanted_title in found_title or found_title in wanted_title):
        score += 2

    artists = track.get("artists")
    if isinstance(artists, list):
        artist_names = " ".join(
            str(item.get("name", "")) for item in artists if isinstance(item, dict)
        )
        found_artists = normalize_text(artist_names)
        if wanted_artist and wanted_artist in found_artists:
            score += 5
        elif wanted_artist and any(part and part in found_artists for part in wanted_artist.split()):
            score += 2

    return score


@lru_cache(maxsize=256)
def fetch_yandex_track_info(title: str, artist: str, album: str) -> YandexTrackInfo:
    query = " ".join(part for part in (title, artist, album) if part).strip()
    if not query:
        return YandexTrackInfo(cover_url=None, track_url=None)

    url = f"https://music.yandex.ru/handlers/music-search.jsx?text={quote_plus(query)}&type=tracks"
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 yd-discord-presence/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=4) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError) as exc:
        logging.debug("Unable to fetch Yandex track info: %s", exc)
        return YandexTrackInfo(cover_url=None, track_url=yandex_search_url(title, artist, album))

    tracks = data.get("tracks", {}).get("items", [])
    if not isinstance(tracks, list) or not tracks:
        return YandexTrackInfo(cover_url=None, track_url=yandex_search_url(title, artist, album))

    best_track = max(
        (track for track in tracks if isinstance(track, dict)),
        key=lambda item: yandex_track_score(item, title, artist),
        default=None,
    )
    if best_track is None:
        return YandexTrackInfo(cover_url=None, track_url=yandex_search_url(title, artist, album))

    return YandexTrackInfo(
        cover_url=cover_uri_to_url(extract_cover_uri(best_track) or ""),
        track_url=extract_track_url(best_track, title, artist, album),
    )


def fetch_yandex_cover_url(title: str, artist: str, album: str) -> str | None:
    return fetch_yandex_track_info(title, artist, album).cover_url


async def read_thumbnail_bytes(thumbnail_ref: Any) -> bytes | None:
    if thumbnail_ref is None:
        return None

    stream = None
    try:
        stream = await asyncio.wait_for(thumbnail_ref.open_read_async(), timeout=2)
        size = int(getattr(stream, "size", 0) or 0)
        if size <= 0:
            return None
        if size > MAX_COVER_BYTES:
            logging.debug("Skipping a thumbnail larger than %s bytes.", MAX_COVER_BYTES)
            return None

        buffer = Buffer(size)
        read_buffer = await asyncio.wait_for(
            stream.read_async(buffer, size, InputStreamOptions.READ_AHEAD),
            timeout=2,
        )
        length = int(getattr(read_buffer, "length", 0) or 0)
        if length <= 0:
            return None

        reader = DataReader.from_buffer(read_buffer)
        try:
            raw_bytes = reader.read_bytes(length)
            return bytes(raw_bytes)
        except TypeError:
            raw_bytes = bytearray(length)
            reader.read_bytes(raw_bytes)
            return bytes(raw_bytes)
    except (asyncio.TimeoutError, OSError, RuntimeError, ValueError) as exc:
        logging.debug("Unable to read media thumbnail: %s", exc)
        return None
    finally:
        if stream is not None and hasattr(stream, "close"):
            try:
                stream.close()
            except OSError:
                pass


async def session_to_track(
    session: GlobalSystemMediaTransportControlsSession,
    config: dict[str, Any],
    cover_server: CoverServer | None,
) -> Track | None:
    try:
        media = await session.try_get_media_properties_async()
        playback = session.get_playback_info()
        timeline = session.get_timeline_properties()
    except OSError as exc:
        logging.debug("Unable to read media session: %s", exc)
        return None

    source = str(getattr(session, "source_app_user_model_id", "") or "")
    unknown_title = str(get_nested(config, "fallback_text.unknown_title", "Unknown track"))
    unknown_artist = str(get_nested(config, "fallback_text.unknown_artist", "Unknown artist"))

    title = str(getattr(media, "title", "") or "").strip() or unknown_title
    artist = str(getattr(media, "artist", "") or "").strip() or unknown_artist
    album = str(getattr(media, "album_title", "") or "").strip()

    status = getattr(playback, "playback_status", None)
    is_playing = status == PlaybackStatus.PLAYING

    start_seconds = timespan_to_seconds(getattr(timeline, "start_time", None)) or 0
    position_seconds = timespan_to_seconds(getattr(timeline, "position", None))
    end_seconds = timespan_to_seconds(getattr(timeline, "end_time", None))
    duration_seconds = None

    if end_seconds is not None:
        duration_seconds = max(0, end_seconds - start_seconds)
    if position_seconds is not None:
        position_seconds = max(0, position_seconds - start_seconds)
        position_seconds = current_timeline_position_seconds(
            position_seconds,
            getattr(timeline, "last_updated_time", None),
            is_playing,
        )
        if position_seconds is not None and duration_seconds is not None:
            position_seconds = min(position_seconds, duration_seconds)

    cover_url = None
    track_url = None
    cover_config = config.get("cover_art", {})
    if not isinstance(cover_config, dict):
        cover_config = {}

    if bool(cover_config.get("enabled", True)):
        if bool(cover_config.get("search_yandex_public_cover", True)):
            yandex_info = await asyncio.to_thread(fetch_yandex_track_info, title, artist, album)
            cover_url = yandex_info.cover_url
            track_url = yandex_info.track_url

        if cover_url is None and cover_server is not None and cover_server.enabled and cover_server.public_base_url:
            thumbnail_bytes = await read_thumbnail_bytes(getattr(media, "thumbnail", None))
            cover_url = cover_server.save(thumbnail_bytes)

    return Track(
        source=source,
        title=title,
        artist=artist,
        album=album,
        is_playing=is_playing,
        position_seconds=position_seconds,
        duration_seconds=duration_seconds,
        cover_url=cover_url,
        track_url=track_url,
    )


def session_matches(session: GlobalSystemMediaTransportControlsSession, filters: list[str]) -> bool:
    if not filters:
        return True
    source = str(getattr(session, "source_app_user_model_id", "") or "").lower()
    return any(item in source for item in filters)


async def get_yandex_track(config: dict[str, Any], cover_server: CoverServer | None) -> Track | None:
    manager = await MediaManager.request_async()
    filters = [str(item).strip().lower() for item in config.get("source_filters", []) if str(item).strip()]

    sessions = list(manager.get_sessions())
    preferred = [session for session in sessions if session_matches(session, filters)]
    current = manager.get_current_session()

    ordered_sessions: list[GlobalSystemMediaTransportControlsSession] = []
    if current is not None and session_matches(current, filters):
        ordered_sessions.append(current)
    ordered_sessions.extend(session for session in preferred if session not in ordered_sessions)

    for session in ordered_sessions:
        track = await session_to_track(session, config, cover_server)
        if track is not None:
            return track

    if sessions:
        logging.info(
            "No media session matched filters %s. Active sources: %s",
            filters,
            ", ".join(str(getattr(session, "source_app_user_model_id", "")) for session in sessions),
        )

    return None


def trim_text(value: str, limit: int = 128) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"

    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_progress_bar(track: Track, config: dict[str, Any]) -> str:
    progress_config = config.get("progress_bar", {})
    if not isinstance(progress_config, dict):
        progress_config = {}

    if not bool(progress_config.get("enabled", True)):
        return f"{format_seconds(track.position_seconds)} / {format_seconds(track.duration_seconds)}"

    length = int(progress_config.get("length", 12) or 12)
    length = max(6, min(length, 24))
    filled_char = str(progress_config.get("filled_char", "━") or "━")[:1]
    empty_char = str(progress_config.get("empty_char", "─") or "─")[:1]
    cursor_char = str(progress_config.get("cursor_char", "●") or "●")[:1]
    show_time = bool(progress_config.get("show_time", True))

    position = max(0, float(track.position_seconds or 0))
    duration = float(track.duration_seconds or 0)
    if duration <= 0:
        return format_seconds(position)

    ratio = max(0.0, min(position / duration, 1.0))
    cursor_index = min(length - 1, max(0, round(ratio * (length - 1))))
    bar = filled_char * cursor_index + cursor_char + empty_char * (length - cursor_index - 1)

    if show_time:
        return f"{format_seconds(position)} {bar} {format_seconds(duration)}"
    return bar


def render_template(
    template: str,
    track: Track,
    config: dict[str, Any],
    extra_fields: dict[str, str] | None = None,
) -> str:
    fields = {
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "source": track.source,
        "position": format_seconds(track.position_seconds),
        "duration": format_seconds(track.duration_seconds),
        "progress": f"{format_seconds(track.position_seconds)} / {format_seconds(track.duration_seconds)}",
        "progress_bar": format_progress_bar(track, config),
        "status": "Playing" if track.is_playing else "Paused",
        "track_url": track.track_url or "",
    }
    if extra_fields:
        fields.update(extra_fields)
    try:
        return trim_text(template.format(**fields))
    except KeyError as exc:
        logging.warning("Unknown template field %s in %r.", exc, template)
        return trim_text(template)


def resolve_activity_type(value: Any) -> ActivityType | None:
    if isinstance(value, ActivityType):
        return value
    if isinstance(value, int):
        for activity_type in ActivityType:
            if activity_type.value == value:
                return activity_type
        return None

    normalized = str(value or "").strip().lower()
    if normalized in {"", "none", "default"}:
        return None
    if normalized in {"playing", "play", "game", "0"}:
        return ActivityType.PLAYING
    if normalized in {"listening", "listen", "music", "2"}:
        return ActivityType.LISTENING
    if normalized in {"watching", "watch", "3"}:
        return ActivityType.WATCHING
    if normalized in {"competing", "compete", "5"}:
        return ActivityType.COMPETING
    logging.warning("Unknown activity_type %r. Using Discord default.", value)
    return None


def pause_extra_fields(pause_state: PauseState | None) -> dict[str, str]:
    elapsed = pause_state.elapsed_seconds() if pause_state is not None else 0
    return {"pause_elapsed": format_seconds(elapsed)}


def presence_payload(
    track: Track,
    config: dict[str, Any],
    pause_state: PauseState | None = None,
) -> dict[str, Any]:
    details_template = str(get_nested(config, "status_template.details", "{title}"))
    state_template = str(get_nested(config, "status_template.state", "{artist}"))
    extra_fields: dict[str, str] | None = None

    pause_config = config.get("pause", {})
    if not isinstance(pause_config, dict):
        pause_config = {}
    if not track.is_playing:
        state_template = str(pause_config.get("state_template", "На паузе {pause_elapsed} • {progress}"))
        extra_fields = pause_extra_fields(pause_state)

    payload: dict[str, Any] = {
        "details": render_template(details_template, track, config, extra_fields),
        "state": render_template(state_template, track, config, extra_fields),
    }
    activity_type = resolve_activity_type(config.get("activity_type", "listening"))
    if activity_type is not None:
        payload["activity_type"] = activity_type

    assets = config.get("assets", {})
    if not isinstance(assets, dict):
        assets = {}

    large_image = track.cover_url
    if not large_image:
        large_image = str(get_nested(config, "cover_art.fallback_large_image", "")).strip()
    if not large_image:
        large_image = str(assets.get("large_image", "")).strip()

    large_text = str(assets.get("large_text", "")).strip()
    small_key = "small_image_playing" if track.is_playing else "small_image_paused"
    small_text_key = "small_text_playing" if track.is_playing else "small_text_paused"
    small_image = str(assets.get(small_key, "")).strip()
    small_text = str(assets.get(small_text_key, "")).strip()
    if not track.is_playing:
        small_text_template = str(pause_config.get("small_text_template", "")).strip()
        if small_text_template:
            small_text = render_template(small_text_template, track, config, extra_fields)

    if large_image:
        payload["large_image"] = large_image
    if large_text:
        payload["large_text"] = trim_text(large_text)
    if small_image:
        payload["small_image"] = small_image
    if small_text:
        payload["small_text"] = trim_text(small_text)

    track_button = config.get("track_button", {})
    normalized_buttons = []
    if isinstance(track_button, dict) and bool(track_button.get("enabled", True)) and track.track_url:
        label = str(track_button.get("label", "Open track")).strip() or "Open track"
        normalized_buttons.append({"label": trim_text(label, 32), "url": track.track_url})

    buttons = config.get("buttons", [])
    if isinstance(buttons, list):
        for button in buttons:
            if not isinstance(button, dict):
                continue
            label = str(button.get("label", "")).strip()
            url = str(button.get("url", "")).strip()
            if label and url:
                normalized_buttons.append({"label": trim_text(label, 32), "url": url})
            if len(normalized_buttons) >= 2:
                break

    if normalized_buttons:
        payload["buttons"] = normalized_buttons[:2]

    timestamp_mode = str(config.get("timestamp_mode", "both")).strip().lower()
    if track.is_playing and track.position_seconds is not None and timestamp_mode not in {"", "none", "off"}:
        now = int(time.time())
        position = int(track.position_seconds)
        remaining = None
        if track.duration_seconds is not None:
            remaining = int(track.duration_seconds - track.position_seconds)

        if timestamp_mode in {"elapsed", "start"}:
            payload["start"] = now - position
        elif timestamp_mode in {"remaining", "end"} and remaining is not None and remaining > 1:
            payload["end"] = now + remaining
        elif timestamp_mode in {"both", "start_end"}:
            payload["start"] = now - position
            if remaining is not None and remaining > 1:
                payload["end"] = now + remaining

    return payload


def payload_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    buttons = payload.get("buttons", [])
    if isinstance(buttons, list):
        button_signature = tuple(
            (button.get("label"), button.get("url"))
            for button in buttons
            if isinstance(button, dict)
        )
    else:
        button_signature = ()

    return (
        payload.get("details"),
        payload.get("state"),
        payload.get("large_image"),
        payload.get("large_text"),
        payload.get("small_image"),
        payload.get("small_text"),
        payload.get("activity_type"),
        button_signature,
    )


def timestamp_signature(payload: dict[str, Any]) -> tuple[int | None, int | None] | None:
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, int):
        start = None
    if not isinstance(end, int):
        end = None
    if start is None and end is None:
        return None
    return start, end


def timestamp_drifted(
    payload: dict[str, Any],
    last_timestamp_signature: tuple[int | None, int | None] | None,
    threshold_seconds: int,
) -> bool:
    current_timestamp_signature = timestamp_signature(payload)
    if current_timestamp_signature is None and last_timestamp_signature is None:
        return False
    if current_timestamp_signature is None or last_timestamp_signature is None:
        return True

    for current_value, last_value in zip(current_timestamp_signature, last_timestamp_signature):
        if current_value is None and last_value is None:
            continue
        if current_value is None or last_value is None:
            return True
        if abs(current_value - last_value) > threshold_seconds:
            return True

    return False


def should_update_presence(
    track: Track,
    payload: dict[str, Any],
    last_signature: tuple[Any, ...] | None,
    last_payload_signature: tuple[Any, ...] | None,
    last_timestamp_signature: tuple[int | None, int | None] | None,
    seek_update_threshold_seconds: int,
    force_update: bool = False,
) -> bool:
    if force_update:
        return True

    if track.signature != last_signature:
        return True

    if payload_signature(payload) != last_payload_signature:
        return True

    if timestamp_drifted(payload, last_timestamp_signature, seek_update_threshold_seconds):
        return True

    return False


async def run() -> None:
    config = load_config()
    poll_interval = max(2, int(config.get("poll_interval_seconds", 3)))
    show_when_paused = bool(config.get("show_when_paused", True))

    cover_server = CoverServer(config)
    cover_server.start()

    presence = DiscordPresenceClient(config["discord_client_id"])
    last_signature: tuple[Any, ...] | None = None
    last_payload_signature: tuple[Any, ...] | None = None
    last_timestamp_signature: tuple[int | None, int | None] | None = None
    seek_update_threshold_seconds = max(1, int(config.get("seek_update_threshold_seconds", 3)))
    pause_config = config.get("pause", {})
    if not isinstance(pause_config, dict):
        pause_config = {}
    pause_update_interval_seconds = max(1, int(pause_config.get("update_interval_seconds", 5)))
    pause_state: PauseState | None = None
    last_pause_update_at = 0.0
    missing_since: float | None = None
    loading_grace_seconds = max(0, int(config.get("loading_grace_seconds", 45)))
    cleared = False
    stop_event = asyncio.Event()

    def stop() -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop())
        except ValueError:
            pass

    logging.info("Watching Yandex Music media sessions.")
    try:
        while not stop_event.is_set():
            try:
                track = await get_yandex_track(config, cover_server)
            except Exception as exc:
                logging.warning("Failed to read Windows media sessions: %s", exc)
                track = None

            if track is None:
                now = time.time()
                if missing_since is None:
                    missing_since = now

                has_previous_presence = last_signature is not None and not cleared
                still_in_grace = now - missing_since < loading_grace_seconds
                if has_previous_presence and still_in_grace:
                    logging.debug(
                        "No track while loading. Keeping previous Discord activity for %.0f more seconds.",
                        loading_grace_seconds - (now - missing_since),
                    )
                elif not cleared:
                    await presence.clear()
                    last_signature = None
                    last_payload_signature = None
                    last_timestamp_signature = None
                    pause_state = None
                    last_pause_update_at = 0.0
                    missing_since = None
                    cleared = True
                    logging.info("No matching active Yandex Music session. Discord activity cleared.")
            elif not track.is_playing and not show_when_paused:
                missing_since = None
                if not cleared:
                    await presence.clear()
                    last_signature = None
                    last_payload_signature = None
                    last_timestamp_signature = None
                    pause_state = None
                    last_pause_update_at = 0.0
                    cleared = True
                    logging.info("No matching active Yandex Music session. Discord activity cleared.")
            else:
                missing_since = None
                force_update = False
                if track.is_playing:
                    if pause_state is not None:
                        pause_state = None
                        last_pause_update_at = 0.0
                        force_update = True
                else:
                    if pause_state is None or pause_state.track_signature != track.signature:
                        pause_state = PauseState(
                            track_signature=track.signature,
                            started_at=time.time(),
                            position_seconds=track.position_seconds,
                        )
                        last_pause_update_at = 0.0
                        force_update = True
                    elif time.time() - last_pause_update_at >= pause_update_interval_seconds:
                        force_update = True

                    if pause_state.position_seconds is not None:
                        track = Track(
                            source=track.source,
                            title=track.title,
                            artist=track.artist,
                            album=track.album,
                            is_playing=track.is_playing,
                            position_seconds=pause_state.position_seconds,
                            duration_seconds=track.duration_seconds,
                            cover_url=track.cover_url,
                            track_url=track.track_url,
                        )

                payload = presence_payload(track, config, pause_state)
                if should_update_presence(
                    track,
                    payload,
                    last_signature,
                    last_payload_signature,
                    last_timestamp_signature,
                    seek_update_threshold_seconds,
                    force_update,
                ):
                    if await presence.update(payload):
                        status = "playing" if track.is_playing else "paused"
                        cover_status = "cover" if track.cover_url else "no cover"
                        logging.info("%s: %s - %s [%s, %s]", status, track.artist, track.title, track.source, cover_status)
                        last_signature = track.signature
                        last_payload_signature = payload_signature(payload)
                        last_timestamp_signature = timestamp_signature(payload)
                        if not track.is_playing:
                            last_pause_update_at = time.time()
                        cleared = False

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await presence.clear()
        cover_server.stop()
        logging.info("Stopped.")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


if __name__ == "__main__":
    configure_logging()
    if sys.platform != "win32":
        raise SystemExit("This client uses Windows media sessions and only works on Windows.")
    asyncio.run(run())
