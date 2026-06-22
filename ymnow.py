# description: Yandex Music — now playing, charts, inline search
# author: @vsecoder
# version: 1.1.0
# requires: yandex-music

from __future__ import annotations

__version__ = "1.1.0"


import asyncio
import json
import random
import string
from typing import Any, Iterable

import aiohttp
from aiogram import types
from yandex_music import ClientAsync  # type: ignore

import tensai
from tensai import types as tensai_types
from tensai.decorators import inline_command
from tensai.loader import Module
from tensai.utils.keyboard import Button, Url
from tensai.utils.entity import escape_html

YNISON_REDIRECTOR_URL = (
    "wss://ynison.music.yandex.ru/redirector.YnisonRedirectService/GetRedirectToYnison"
)
YNISON_ORIGIN = "http://music.yandex.ru"
YNISON_DEVICE_TYPE = 1

# Lyrics-info attributes that, when truthy, mean the track has lyrics.
LYRICS_FLAGS = (
    "has_lyrics",
    "has_available_lyrics",
    "has_full_lyrics",
    "has_sync_lyrics",
)
# Filename characters disallowed by Windows.
WINDOWS_BAD_FILENAME_CHARS = '<>:"/\\|?*'


def _attr_or_dict(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from either an attribute (sdk model) or a dict.

    The yandex-music SDK returns Pydantic-style objects from some endpoints
    and plain dicts from others; this helper papers over the difference so
    callers do not have to ``getattr`` then ``isinstance(... , dict)`` for
    every field.
    """
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return default if value is None else value


# https://github.com/FozerG/YandexMusicRPC/blob/main/main.py#L133
async def get_current_track(client: ClientAsync, token: str) -> dict[str, Any]:
    if not token:
        return {"success": False, "error": "No token"}

    device_info = {
        "app_name": "Chrome",
        "type": 1,
    }

    ws_proto = {
        "Ynison-Device-Id": "".join(
            [random.choice(string.ascii_lowercase) for _ in range(16)]
        ),
        "Ynison-Device-Info": json.dumps(device_info),
    }

    timeout = aiohttp.ClientTimeout(total=15, connect=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(
                url="wss://ynison.music.yandex.ru/redirector.YnisonRedirectService/GetRedirectToYnison",
                headers={
                    "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(ws_proto)}",
                    "Origin": "http://music.yandex.ru",
                    "Authorization": f"OAuth {token}",
                },
                timeout=10,
            ) as ws:
                recv = await ws.receive()
                data = json.loads(recv.data)

            if "redirect_ticket" not in data or "host" not in data:
                return {"success": False, "error": "Invalid response"}

            new_ws_proto = ws_proto.copy()
            new_ws_proto["Ynison-Redirect-Ticket"] = data["redirect_ticket"]

            to_send = {
                "update_full_state": {
                    "player_state": {
                        "player_queue": {
                            "current_playable_index": -1,
                            "entity_id": "",
                            "entity_type": "VARIOUS",
                            "playable_list": [],
                            "options": {"repeat_mode": "NONE"},
                            "entity_context": "BASED_ON_ENTITY_BY_DEFAULT",
                            "version": {
                                "device_id": ws_proto["Ynison-Device-Id"],
                                "version": 9021243204784341000,
                                "timestamp_ms": 0,
                            },
                            "from_optional": "",
                        },
                        "status": {
                            "duration_ms": 0,
                            "paused": True,
                            "playback_speed": 1,
                            "progress_ms": 0,
                            "version": {
                                "device_id": ws_proto["Ynison-Device-Id"],
                                "version": 8321822175199937000,
                                "timestamp_ms": 0,
                            },
                        },
                    },
                    "device": {
                        "capabilities": {
                            "can_be_player": True,
                            "can_be_remote_controller": False,
                            "volume_granularity": 16,
                        },
                        "info": {
                            "device_id": ws_proto["Ynison-Device-Id"],
                            "type": "WEB",
                            "title": "Chrome Browser",
                            "app_name": "Chrome",
                        },
                        "volume_info": {"volume": 0},
                        "is_shadow": True,
                    },
                    "is_currently_active": False,
                },
                "rid": "ac281c26-a047-4419-ad00-e4fbfda1cba3",
                "player_action_timestamp_ms": 0,
                "activity_interception_type": "DO_NOT_INTERCEPT_BY_DEFAULT",
            }

            async with session.ws_connect(
                url=f"wss://{data['host']}/ynison_state.YnisonStateService/PutYnisonState",
                headers={
                    "Sec-WebSocket-Protocol": f"Bearer, v2, {json.dumps(new_ws_proto)}",
                    "Origin": "http://music.yandex.ru",
                    "Authorization": f"OAuth {token}",
                },
                timeout=10,
                method="GET",
            ) as ws:
                await ws.send_str(json.dumps(to_send))
                recv = await asyncio.wait_for(ws.receive(), timeout=10)
                ynison = json.loads(recv.data)
                track_index = ynison["player_state"]["player_queue"][
                    "current_playable_index"
                ]
                if track_index == -1:
                    return {"success": False, "error": "No track playing"}
                track = ynison["player_state"]["player_queue"]["playable_list"][
                    track_index
                ]

        info = await client.tracks_download_info(track["playable_id"], True)
        track = await client.tracks(track["playable_id"])
        return {
            "paused": ynison["player_state"]["status"]["paused"],
            "duration_ms": ynison["player_state"]["status"]["duration_ms"],
            "progress_ms": ynison["player_state"]["status"]["progress_ms"],
            "entity_id": ynison["player_state"]["player_queue"]["entity_id"],
            "repeat_mode": ynison["player_state"]["player_queue"]["options"][
                "repeat_mode"
            ],
            "entity_type": ynison["player_state"]["player_queue"]["entity_type"],
            "track": track,
            "info": info,
            "success": True,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "track": None}


class Ymnow(Module):
    config = tensai_types.ModuleConfig(
        tensai_types.ConfigValue(
            key="token",
            name="Yandex.Music token",
            default="",
            type=tensai_types.HiddenType(tensai_types.StringType()),
            description="OAuth token for Yandex.Music.",
        ),
        tensai_types.ConfigValue(
            key="show_link",
            name="Show song.link",
            default=True,
            type=tensai_types.BoolType(),
            description="Show song.link button with track URL.",
        ),
    )

    strings = {
        "ru": {
            "loading": "<b>Загружаю...</b>",
            "now_playing": (
                "<b>🎶 Сейчас играет:</b>\n"
                "<code>{artists}</code> - <code>{title}</code>\n"
                ":e:clock <code>{duration}</code>"
            ),
            "no_track": ":e:cross Сейчас ничего не играет",
            "error": ":e:alert Ошибка при получении трека: {error}",
            "no_token": (
                ":e:key Токен Яндекс.Музыки не установлен. Задайте его в "
                "<code>config</code> → ymnow → token\n"
                "Instructions: https://github.com/MarshalX/yandex-music-api/discussions/513#discussioncomment-2729781"
            ),
            "no_query": "Введите запрос после <code>ym</code>.",
            "no_results": "Ничего не найдено.",
            "like": "👍",
            "dislike": "👎",
            "lyrics": "📝",
            "chart": "🏆 Чарт: {title}",
            "search": "🔎 Поиск: {query}",
            "liked": "👍 Лайк поставлен",
            "disliked": "👎 Дизлайк поставлен",
            "lyrics_failed": ":e:alert Не удалось получить текст",
            "like_failed": ":e:alert Не удалось поставить лайк",
            "dislike_failed": ":e:alert Не удалось поставить дизлайк",
        },
        "en": {
            "loading": "<b>Loading...</b>",
            "now_playing": (
                "<b>🎶 Now playing:</b>\n"
                "<code>{artists}</code> - <code>{title}</code>\n"
                ":e:clock <code>{duration}</code>"
            ),
            "no_track": ":e:cross Nothing is playing now",
            "error": ":e:alert Error getting track: {error}",
            "no_token": (
                ":e:key Yandex.Music token is not set. Set it via "
                "<code>config</code> → ymnow → token\n"
                "Instructions: https://github.com/MarshalX/yandex-music-api/discussions/513#discussioncomment-2729781"
            ),
            "no_query": "Type a query after <code>ym</code>.",
            "no_results": "Nothing found.",
            "like": "👍",
            "dislike": "👎",
            "lyrics": "📝",
            "chart": "🏆 Chart: {title}",
            "search": "🔎 Search: {query}",
            "liked": "👍 Liked",
            "disliked": "👎 Disliked",
            "lyrics_failed": ":e:alert Failed to get lyrics",
            "like_failed": ":e:alert Failed to like",
            "dislike_failed": ":e:alert Failed to dislike",
        },
    }

    async def on_load(self) -> None:
        # Loader.initialize_config() seeds defaults before on_load runs.
        if self.mdb.get("legacy_migrated"):
            return
        legacy = self.mdb.get("token")
        if isinstance(legacy, str) and legacy and not self._get_token():
            try:
                self.config.set("token", legacy)
                self.mdb.set("token", None)
            except Exception:
                pass
        self.mdb.set("legacy_migrated", True)

    def _get_token(self) -> str | None:
        token = self.config.get("token")
        token = token.strip() if isinstance(token, str) else ""
        return token or None

    def _format_duration(self, duration_ms: int) -> str:
        seconds = int(duration_ms // 1000)
        return f"{seconds // 60:02}:{seconds % 60:02}"

    def _media_url(self, value: str | None, size: str | int = 400) -> str | None:
        if not value:
            return None
        url = value.replace("%%", str(size))
        if url.startswith("//"):
            url = f"https:{url}"
        elif not url.startswith("http"):
            url = f"https://{url}"
        return url

    def _has_lyrics(self, track) -> bool:
        info = _attr_or_dict(track, "lyrics_info")
        if not info:
            return False
        return any(_attr_or_dict(info, flag) for flag in LYRICS_FLAGS)

    def _safe_filename(self, name: str) -> str:
        return "".join(
            "_" if ch in WINDOWS_BAD_FILENAME_CHARS else ch for ch in name
        ).strip()

    def _track_media(
        self, track: Any, *, thumb_size: str = "100x100", video_size: int = 360
    ) -> tuple[str | None, str | None]:
        """Return ``(thumb_url, video_url)`` for any track-like object."""
        cover = _attr_or_dict(track, "cover_uri")
        og_image = _attr_or_dict(track, "og_image")
        thumb_url = self._media_url(cover, thumb_size) or self._media_url(
            og_image, thumb_size
        )
        background = _attr_or_dict(track, "background_video_uri")
        video_url = self._media_url(background, video_size)
        return thumb_url, video_url

    def _track_artists(self, track: Any) -> str:
        artists = _attr_or_dict(track, "artists") or []
        names: list[str] = []
        for artist in artists:
            name = _attr_or_dict(artist, "name")
            if name:
                names.append(name)
        return ", ".join(names)

    async def _search_tracks(self, token: str, query: str, limit: int = 8):
        async with self._client(token) as client:
            result = await client.search(query)
            tracks = (result.tracks.results or []) if result and result.tracks else []
            return await self._tracks_to_items(client, tracks[:limit])

    def _track_fields(self, track) -> tuple[str | None, str | None, str]:
        track_id = _attr_or_dict(track, "id")
        title = _attr_or_dict(track, "title")
        return (
            str(track_id) if track_id is not None else None,
            title,
            self._track_artists(track),
        )

    async def _now_playing_inline(self, token: str):
        async with self._client(token) as client:
            result = await get_current_track(client, token)
        if not result.get("success"):
            return None
        track = result["track"][0]
        info = result["info"][0] if result.get("info") else None
        if not info:
            return None
        link = _attr_or_dict(info, "direct_link")
        if not link:
            return None
        track_id, title, artists = self._track_fields(track)
        thumb_url, video_url = self._track_media(track)
        has_lyrics = self._has_lyrics(track)
        filename = _attr_or_dict(info, "filename")
        return (
            track_id,
            title,
            artists,
            link,
            thumb_url,
            video_url,
            has_lyrics,
            filename,
        )

    async def _chart_tracks(self, token: str, chart_id: str, limit: int = 8):
        async with self._client(token) as client:
            chart = await client.chart(chart_id)
            if not chart or not chart.chart:
                return None, []
            items = await self._chart_to_items(client, chart.chart.tracks[:limit])
            return chart.chart, items

    _CHART_PROGRESS_PREFIX = {"down": "🔻", "up": "🔺", "new": "🆕"}

    def _chart_title(self, chart_info: Any, title: str) -> str:
        prefix = self._CHART_PROGRESS_PREFIX.get(chart_info.progress)
        if not prefix and chart_info.position == 1:
            prefix = "👑"
        prefixed = f"{prefix} {title}" if prefix else title
        return f"{chart_info.position} {prefixed}"

    async def _resolve_track_download(
        self, client: ClientAsync, track_id: Any
    ) -> tuple[str | None, str | None] | None:
        """Resolve ``(direct_link, filename)`` for a track id, or ``None``."""
        info = await client.tracks_download_info(track_id, True)
        if not info:
            return None
        link = info[0].direct_link
        if not link:
            return None
        return link, _attr_or_dict(info[0], "filename")

    async def _chart_to_items(self, client: ClientAsync, track_items: Iterable):
        items: list[tuple] = []
        for track_short in track_items:
            track = track_short.track
            chart_info = track_short.chart
            resolved = await self._resolve_track_download(client, track.id)
            if resolved is None:
                continue
            link, filename = resolved

            artists = self._track_artists(track)
            title = self._chart_title(chart_info, track.title)
            thumb_url, video_url = self._track_media(track)
            has_lyrics = self._has_lyrics(track)
            items.append(
                (
                    track,
                    artists,
                    link,
                    title,
                    thumb_url,
                    video_url,
                    has_lyrics,
                    filename,
                )
            )
        return items

    async def _tracks_to_items(self, client: ClientAsync, tracks: Iterable):
        items: list[tuple] = []
        for track in tracks:
            resolved = await self._resolve_track_download(client, track.id)
            if resolved is None:
                continue
            link, filename = resolved
            artists = self._track_artists(track)
            thumb_url, video_url = self._track_media(track)
            has_lyrics = self._has_lyrics(track)
            items.append(
                (track, artists, link, thumb_url, video_url, has_lyrics, filename)
            )
        return items

    class _client:
        def __init__(self, token: str) -> None:
            self.token = token
            self.client: ClientAsync | None = None

        async def __aenter__(self) -> ClientAsync:
            self.client = ClientAsync(self.token)
            await self.client.init()
            return self.client

        async def __aexit__(self, exc_type, exc, tb) -> None:
            if self.client:
                try:
                    await self.client.close()
                except Exception:
                    pass

    async def _apply_like_state(
        self,
        callback: types.CallbackQuery,
        track_id: str,
        state: str | None,
        has_lyrics: bool | None = None,
        video_url: str | None = None,
    ) -> None:
        keyboard = self._track_keyboard(
            track_id,
            state=state,
            has_lyrics=True if has_lyrics is None else has_lyrics,
            video_url=video_url,
        )
        try:
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=keyboard)
                return
            if callback.inline_message_id and tensai.bot is not None:
                await tensai.bot.edit_message_reply_markup(
                    inline_message_id=callback.inline_message_id,
                    reply_markup=keyboard,
                )
        except Exception:
            pass

    def _track_keyboard(
        self,
        track_id: str | int,
        state: str | None = None,
        has_lyrics: bool = True,
        video_url: str | None = None,
        poster_url: str | None = None,
    ) -> types.InlineKeyboardMarkup:
        like_text = self.strings("like")
        dislike_text = self.strings("dislike")
        if state == "like":
            like_text = f"{like_text} ✅"
        elif state == "dislike":
            dislike_text = f"{dislike_text} ✅"
        row = [
            Button(like_text, on_click=self._make_ym_click("like", track_id)),
            Button(dislike_text, on_click=self._make_ym_click("dislike", track_id)),
        ]
        if has_lyrics:
            row.append(
                Button(
                    self.strings("lyrics"),
                    on_click=self._make_ym_click("lyrics", track_id),
                )
            )
        keyboard: list[list[Button | Url]] = [row]
        media_row: list[Button | Url] = []
        if poster_url:
            media_row.append(Url("🖼", poster_url))
        if video_url:
            media_row.append(Url("🎬", video_url))
        if media_row:
            keyboard.append(media_row)
        return self.keyboard(keyboard)

    def _make_ym_click(self, action: str, track_id: str | int):
        """on_click factory — action/track are baked into the closure."""

        async def _click(_module, callback: types.CallbackQuery) -> None:
            await self._ym_action(callback, action, str(track_id))

        return _click

    @inline_command(
        aliases=["ym", "ymnow", "ymchart"],
        description={
            "ru": "ym запрос - поиск треков; ymnow - текущий трек; ymchart chart_id - чарты",
            "en": "ym query - search tracks; ymnow - now playing; ymchart chart_id - charts",
        },
    )
    async def _inlinecmd_ym(self, inline_query: types.InlineQuery):
        query = getattr(inline_query, "query", "") or ""
        parts = query.split(maxsplit=1)
        first = parts[0] if parts else ""
        search_query = parts[1].strip() if len(parts) > 1 else ""
        if not search_query and first != "ymnow":
            return await self.answer(
                inline_query,
                inline_results=[],
                inline_is_personal=True,
                inline_cache_time=0,
            )

        token = self._get_token()
        if not token:
            article = types.inline_query_result_article.InlineQueryResultArticle(
                id="no_token",
                title=self.strings("no_token"),
                description=self.strings("no_token"),
                input_message_content=types.input_text_message_content.InputTextMessageContent(
                    message_text=self.strings("no_token")
                ),
            )
            return await self.answer(
                inline_query,
                inline_results=[article],
                inline_is_personal=True,
                inline_cache_time=0,
            )

        if first == "ymnow":
            now = await self._now_playing_inline(token)
            if not now:
                article = types.inline_query_result_article.InlineQueryResultArticle(
                    id="no_results",
                    title=self.strings("no_track"),
                    description=self.strings("no_track"),
                    input_message_content=types.input_text_message_content.InputTextMessageContent(
                        message_text=self.strings("no_track")
                    ),
                )
                return await self.answer(
                    inline_query,
                    inline_results=[article],
                    inline_is_personal=True,
                    inline_cache_time=0,
                )
            (
                track_id,
                title,
                artists,
                link,
                thumb_url,
                video_url,
                has_lyrics,
                filename,
            ) = now
            thumbnail_url = thumb_url
            result = types.inline_query_result_audio.InlineQueryResultAudio(
                id=str(track_id),
                audio_url=link,
                title=title or "Track",
                performer=artists or None,
                thumbnail_url=thumbnail_url,
                reply_markup=self._track_keyboard(
                    track_id,
                    has_lyrics=has_lyrics,
                    video_url=video_url,
                    poster_url=thumb_url,
                ),
            )
            return await self.answer(
                inline_query,
                inline_results=[result],
                inline_is_personal=True,
                inline_cache_time=0,
            )

        if first == "ymchart":
            chart_id = search_query or "world"
            chart, items = await self._chart_tracks(token, chart_id, limit=8)
            if not items:
                article = types.inline_query_result_article.InlineQueryResultArticle(
                    id="no_results",
                    title=self.strings("no_results"),
                    description=self.strings("no_results"),
                    input_message_content=types.input_text_message_content.InputTextMessageContent(
                        message_text=self.strings("no_results")
                    ),
                )
                return await self.answer(
                    inline_query,
                    inline_results=[article],
                    inline_is_personal=True,
                    inline_cache_time=0,
                )
            title = chart.title if chart else chart_id
            results: list[types.InlineQueryResultAudio] = []
            for (
                track,
                artists,
                link,
                chart_title,
                thumb_url,
                video_url,
                has_lyrics,
                filename,
            ) in items:
                thumbnail_url = thumb_url
                result = types.inline_query_result_audio.InlineQueryResultAudio(
                    id=f"chart_{track.id}",
                    audio_url=link,
                    title=chart_title,
                    performer=artists or None,
                    thumbnail_url=thumbnail_url,
                    reply_markup=self._track_keyboard(
                        track.id,
                        has_lyrics=has_lyrics,
                        video_url=video_url,
                        poster_url=thumb_url,
                    ),
                )
                results.append(result)
            header = types.inline_query_result_article.InlineQueryResultArticle(
                id=f"chart_header_{chart_id}",
                title=self.strings("chart").format(title=title),
                description=getattr(chart, "description", "") if chart else "",
                input_message_content=types.input_text_message_content.InputTextMessageContent(
                    message_text=self.strings("chart").format(title=title)
                ),
            )
            return await self.answer(
                inline_query,
                inline_results=[header, *results],
                inline_is_personal=True,
                inline_cache_time=0,
            )

        items = await self._search_tracks(token, search_query)
        if not items:
            article = types.inline_query_result_article.InlineQueryResultArticle(
                id="no_results",
                title=self.strings("no_results"),
                description=self.strings("no_results"),
                input_message_content=types.input_text_message_content.InputTextMessageContent(
                    message_text=self.strings("no_results")
                ),
            )
            return await self.answer(
                inline_query,
                inline_results=[article],
                inline_is_personal=True,
                inline_cache_time=0,
            )

        results: list[types.InlineQueryResultAudio] = []
        for track, artists, link, thumb_url, video_url, has_lyrics, filename in items:
            title = track.title
            thumbnail_url = thumb_url
            result = types.inline_query_result_audio.InlineQueryResultAudio(
                id=str(track.id),
                audio_url=link,
                title=title,
                performer=artists or None,
                thumbnail_url=thumbnail_url,
                reply_markup=self._track_keyboard(
                    track.id,
                    has_lyrics=has_lyrics,
                    video_url=video_url,
                    poster_url=thumb_url,
                ),
            )
            results.append(result)

        header = types.inline_query_result_article.InlineQueryResultArticle(
            id=f"search_header_{search_query}",
            title=self.strings("search").format(query=search_query),
            description=self.strings("search").format(query=search_query),
            input_message_content=types.input_text_message_content.InputTextMessageContent(
                message_text=self.strings("search").format(
                    query=escape_html(search_query)
                )
            ),
        )

        return await self.answer(
            inline_query,
            inline_results=[header, *results],
            inline_is_personal=True,
            inline_cache_time=0,
        )

    async def _ym_action(
        self, callback: types.CallbackQuery, action: str, track_id: str
    ):
        token = self._get_token()
        if not token:
            return await callback.answer(self.strings("no_token"), show_alert=True)

        client = ClientAsync(token)
        try:
            await client.init()
            if action == "like":
                ok = await client.users_likes_tracks_add(track_id)
                text = self.strings("liked") if ok else self.strings("like_failed")
                await callback.answer(text, show_alert=False)
                has_lyrics = None
                video_url = None
                try:
                    track_info = await client.tracks(track_id)
                    track = track_info[0] if track_info else None
                    if track:
                        has_lyrics = self._has_lyrics(track)
                        background = getattr(track, "background_video_uri", None)
                        video_url = self._media_url(background, 360)
                except Exception:
                    pass
                await self._apply_like_state(
                    callback,
                    track_id,
                    "like" if ok else None,
                    has_lyrics=has_lyrics,
                    video_url=video_url,
                )
                return
            if action == "dislike":
                ok = await client.users_likes_tracks_remove(track_id)
                text = (
                    self.strings("disliked") if ok else self.strings("dislike_failed")
                )
                await callback.answer(text, show_alert=False)
                has_lyrics = None
                video_url = None
                try:
                    track_info = await client.tracks(track_id)
                    track = track_info[0] if track_info else None
                    if track:
                        has_lyrics = self._has_lyrics(track)
                        background = getattr(track, "background_video_uri", None)
                        video_url = self._media_url(background, 360)
                except Exception:
                    pass
                await self._apply_like_state(
                    callback,
                    track_id,
                    "dislike" if ok else None,
                    has_lyrics=has_lyrics,
                    video_url=video_url,
                )
                return
            if action == "lyrics":
                lyrics = await client.tracks_lyrics(track_id)
                if lyrics:
                    text = await lyrics.fetch_lyrics_async()
                else:
                    text = None
                if not text:
                    await callback.answer(
                        self.strings("lyrics_failed"), show_alert=True
                    )
                    return
                msg = callback.message
                if msg:
                    if len(text) > 3500:
                        filename = None
                        try:
                            info = await client.tracks_download_info(track_id, True)
                            if info:
                                filename = getattr(info[0], "filename", None)
                                if filename is None and isinstance(info[0], dict):
                                    filename = info[0].get("filename")
                        except Exception:
                            filename = None
                        if filename:
                            base = filename.rsplit(".", 1)[0]
                            filename = f"{self._safe_filename(base)}.txt"
                        else:
                            filename = "lyrics.txt"
                        file = types.BufferedInputFile(
                            text.encode("utf-8"), filename=filename
                        )
                        await msg.answer_document(
                            document=file,
                            caption=self.strings("lyrics"),
                        )
                    else:
                        await msg.answer(f"<pre>{escape_html(text)}</pre>")
                    await callback.answer(self.strings("lyrics"), show_alert=False)
                    return
                short = text.strip().replace("\n", " ")
                if len(short) > 220:
                    short = short[:220].rstrip() + "…"
                await callback.answer(
                    short or self.strings("lyrics_failed"), show_alert=True
                )
        except Exception:
            if action == "like":
                await callback.answer(self.strings("like_failed"), show_alert=True)
            elif action == "dislike":
                await callback.answer(self.strings("dislike_failed"), show_alert=True)
            else:
                await callback.answer(self.strings("lyrics_failed"), show_alert=True)
        finally:
            try:
                await client.close()
            except Exception:
                pass
