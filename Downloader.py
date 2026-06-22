# description: Media downloader — YouTube, Instagram, TikTok, etc.
# author: @vsecoder
# version: 1.1.0
# requires: yt-dlp,aiohttp
#
# System dependency: **ffmpeg** must be on PATH. yt-dlp merges
# separately-streamed audio+video for most modern YouTube formats and
# will abort with "ffmpeg is not installed" otherwise. The bundled
# Dockerfile installs it; on bare-metal setups install via the system
# package manager (``apt install ffmpeg`` / ``brew install ffmpeg`` /
# ``choco install ffmpeg``).

import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import FSInputFile
from yt_dlp import YoutubeDL

from tensai import types as tensai_types
from tensai.decorators import command, inline_command
from tensai.loader import Module
from tensai.utils.keyboard import Url
from tensai.utils.entity import escape_html
from tensai.utils.topics import TopicRegistry

__version__ = "1.1.0"

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"(https?://\S+)")
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
TIKTOK_PREFIXES = (
    "https://www.tiktok.com/",
    "https://m.tiktok.com/",
    "https://vm.tiktok.com/",
    "https://vt.tiktok.com/",
)

# Per-module forum topic for archived downloads (one copy of every
# successfully downloaded file ends up here so the owner gets a
# searchable history). Re-uses the file_id Telegram returned for the
# user-facing send — no extra upload, no extra bandwidth.
DOWNLOADER_TOPIC = "Downloader"


class DownloadTooLarge(Exception):
    pass


@dataclass(slots=True)
class DownloadResult:
    path: Path
    info: dict[str, Any] | None
    title: str | None
    size: int | None = None


class ProgressState:
    def __init__(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.loop = loop
        self.data: dict[str, Any] = {}
        self.version = 0
        self.done = False

    def update(self, data: dict[str, Any]) -> None:
        self.data = data
        self.version += 1

    def update_threadsafe(self, data: dict[str, Any]) -> None:
        if self.loop:
            self.loop.call_soon_threadsafe(self.update, data)
        else:
            self.update(data)

    def finish(self) -> None:
        self.done = True
        self.version += 1


class Downloader(Module):
    config = tensai_types.ModuleConfig(
        tensai_types.ConfigValue(
            key="cookies_file",
            name="Cookies file (yt-dlp)",
            default="",
            type=tensai_types.StringType(),
            description="Path to cookies.txt for YouTube/Instagram.",
        ),
        tensai_types.ConfigValue(
            key="cookies_from_browser",
            name="Cookies from browser",
            default="",
            type=tensai_types.StringType(),
            description="Browser name or profile for yt-dlp (e.g. chrome).",
        ),
        tensai_types.ConfigValue(
            key="max_inline_mb",
            name="Inline max size (MB)",
            default=45,
            type=tensai_types.IntType(),
            description="Max file size for inline downloads.",
        ),
        tensai_types.ConfigValue(
            key="max_upload_mb",
            name="Upload max size (MB)",
            default=200,
            type=tensai_types.IntType(),
            description="Max file size for command downloads.",
        ),
        tensai_types.ConfigValue(
            key="cache_enabled",
            name="Cache downloads",
            default=True,
            type=tensai_types.BoolType(),
            description="Enable DB cache for downloaded files.",
        ),
        tensai_types.ConfigValue(
            key="progress_interval",
            name="Progress update interval (sec)",
            default=5.0,
            type=tensai_types.FloatType(),
            description="How often to update download progress.",
        ),
    )

    strings = {
        "ru": {
            "no_url": "<b>Укажи ссылку.</b>",
            "cache_empty": "<b>Кэш пуст.</b>",
            "cache_list_title": "<b>Кэш (видео):</b>",
            "downloading": "<b>Скачиваю...</b>",
            "download_failed": "<b>Не удалось скачать.</b>",
            "too_large": "<b>Файл слишком большой.</b>",
            "open_pm": "<b>Открой ЛС, чтобы продолжить.</b>",
            "open_url": "Открыть ссылку",
            "sent_dm": "<b>Файл отправлен в личные сообщения.</b>",
            "inline_too_large": "Слишком большой файл — используй команду.",
            "progress": (
                "<b>Скачиваю:</b>\n"
                "<code>{title}</code>\n"
                "Размер: {size}\n"
                "Скорость: {speed}\n"
                "Осталось: {eta}"
            ),
            "unknown": "—",
            "cache_cleared": "<b>Кэш скачанных файлов очищен.</b>",
            "sending": "<b>Скачано. Отправляю...</b>",
        },
        "en": {
            "no_url": "<b>Provide a link.</b>",
            "cache_empty": "<b>Cache is empty.</b>",
            "cache_list_title": "<b>Cache (videos):</b>",
            "downloading": "<b>Downloading...</b>",
            "download_failed": "<b>Download failed.</b>",
            "too_large": "<b>File is too large.</b>",
            "open_pm": "<b>Open PMs to continue.</b>",
            "open_url": "Open link",
            "sent_dm": "<b>Sent to private messages.</b>",
            "inline_too_large": "File is too large — use the command.",
            "progress": (
                "<b>Downloading:</b>\n"
                "<code>{title}</code>\n"
                "Size: {size}\n"
                "Speed: {speed}\n"
                "ETA: {eta}"
            ),
            "unknown": "—",
            "cache_cleared": "<b>Download cache cleared.</b>",
            "sending": "<b>Downloaded. Sending...</b>",
        },
    }

    download_dir: Path = Path("downloads")

    async def on_load(self) -> None:
        # Loader.initialize_config() runs Config.initialize for us.
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def _cache_key(self, url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def _cache_path(self, url: str) -> str:
        return f"downloads.{self._cache_key(url)}"

    def _cache_allowed(self) -> bool:
        try:
            return bool(self.config.get("cache_enabled"))
        except Exception:
            return True

    def _cache_get(self, url: str) -> dict[str, Any] | None:
        if not self._cache_allowed():
            return None
        cached = self.mdb.get(self._cache_path(url))
        return cached if isinstance(cached, dict) else None

    def _cache_set(self, url: str, data: dict[str, Any]) -> None:
        if not self._cache_allowed():
            return
        payload = dict(data)
        payload["url"] = url
        payload["ts"] = time.time()
        key = self._cache_key(url)
        self.mdb.set(self._cache_path(url), payload)
        index = self.mdb.get("downloads")
        if not isinstance(index, dict):
            index = {}
        index[key] = payload
        self.mdb.set("downloads", index)

    def _cache_list_videos(self) -> list[dict[str, Any]]:
        if not self._cache_allowed():
            return []
        data = self.mdb.get("downloads")
        if not isinstance(data, dict):
            return []
        items = []
        for value in data.values():
            if not isinstance(value, dict):
                continue
            if value.get("type") != "video":
                continue
            items.append(value)
        items.sort(key=lambda item: item.get("ts") or 0, reverse=True)
        return items

    def _format_cache_item(self, item: dict[str, Any]) -> str:
        title = item.get("title") or item.get("name") or "video"
        size = item.get("size")
        size_text = (
            self._format_bytes(size)
            if isinstance(size, int)
            else self.strings("unknown")
        )
        return f"• <code>{escape_html(str(title))}</code> ({size_text})"

    def _cfg_int(self, key: str, default: int) -> int:
        value = self.config.get(key)
        if isinstance(value, int) and value > 0:
            return value
        try:
            ivalue = int(value)
        except (TypeError, ValueError):
            return default
        return ivalue if ivalue > 0 else default

    def _cfg_str(self, key: str) -> str:
        value = self.config.get(key)
        return str(value).strip() if value is not None else ""

    def _mb_to_bytes(self, mb: int) -> int:
        return int(mb * 1024 * 1024)

    def _limit_bytes(self, *, inline: bool) -> int:
        key = "max_inline_mb" if inline else "max_upload_mb"
        return self._mb_to_bytes(self._cfg_int(key, 45))

    def _format_bytes(self, size: int | None) -> str:
        if size is None:
            return self.strings("unknown")
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        idx = 0
        while value >= 1024 and idx < len(units) - 1:
            value /= 1024
            idx += 1
        return f"{value:.1f} {units[idx]}"

    def _format_eta(self, seconds: int | None) -> str:
        if seconds is None or seconds < 0:
            return self.strings("unknown")
        minutes, sec = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02}:{minutes:02}:{sec:02}"
        return f"{minutes:02}:{sec:02}"

    def _progress_text(self, data: dict[str, Any]) -> str:
        title = escape_html(data.get("title") or "file")
        downloaded = data.get("downloaded")
        total = data.get("total")
        speed = data.get("speed")
        eta = data.get("eta")
        if total:
            size = f"{self._format_bytes(downloaded)} / {self._format_bytes(total)}"
        else:
            size = f"{self._format_bytes(downloaded)}"
        speed_text = (
            f"{self._format_bytes(int(speed))}/s"
            if isinstance(speed, (int, float)) and speed > 0
            else self.strings("unknown")
        )
        eta_text = (
            self._format_eta(int(eta))
            if isinstance(eta, (int, float))
            else self.strings("unknown")
        )
        return self.strings("progress").format(
            title=title,
            size=size,
            speed=speed_text,
            eta=eta_text,
        )

    async def _progress_worker(
        self,
        message: types.Message,
        state: ProgressState,
    ) -> None:
        interval = 3.0
        try:
            interval = float(self.config.get("progress_interval"))
        except Exception:
            interval = 3.0
        if interval <= 0:
            interval = 3.0
        last_version = -1
        last_edit = 0.0
        last_text = None
        while not state.done:
            if state.version != last_version and state.data:
                now = time.monotonic()
                if now - last_edit >= interval:
                    text = self._progress_text(state.data)
                    if text and text != last_text:
                        try:
                            await message.edit_text(text)
                        except Exception:
                            pass
                        last_text = text
                        last_edit = now
                    last_version = state.version
            await asyncio.sleep(min(max(interval / 2, 0.2), 2.0))

    async def _stop_progress(
        self,
        state: ProgressState | None,
        task: asyncio.Task | None,
    ) -> None:
        if state:
            state.finish()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _extract_url(self, text: str | None) -> str | None:
        if not text:
            return None
        match = URL_RE.search(text)
        return match.group(1) if match else None

    def _is_tiktok_url(self, url: str) -> bool:
        return url.startswith(TIKTOK_PREFIXES)

    def _is_video_file(self, path: Path) -> bool:
        return path.suffix.lower() in VIDEO_EXTS

    def _is_video_info(self, info: dict[str, Any] | None) -> bool | None:
        if not info:
            return None
        vcodec = info.get("vcodec")
        if isinstance(vcodec, str):
            return vcodec != "none"
        ext = info.get("ext")
        if isinstance(ext, str):
            return f".{ext.lower()}" in VIDEO_EXTS
        return None

    def _extract_platform(self, info: dict[str, Any] | None) -> str | None:
        if not info:
            return None
        name = info.get("extractor_key") or info.get("extractor") or info.get("ie_key")
        if not name:
            return None
        normalized = str(name).replace("_", " ").strip().lower()
        known = {
            "youtube": "YouTube",
            "youtu be": "YouTube",
            "tiktok": "TikTok",
            "instagram": "Instagram",
        }
        return known.get(normalized, str(name).replace("_", " ").strip().title())

    def _limit_caption(self, text: str, limit: int = 1024) -> str:
        if len(text) <= limit:
            return text
        if limit <= 1:
            return text[:limit]
        return text[: limit - 1] + "…"

    def _build_caption(
        self,
        info: dict[str, Any] | None,
        url: str | None = None,
        *,
        title: str | None = None,
        description: str | None = None,
        platform: str | None = None,
    ) -> str | None:
        if info:
            platform = platform or self._extract_platform(info)
            title = title or info.get("title")
            description = description or info.get("description")
            url = url or info.get("webpage_url") or info.get("original_url")
        parts: list[str] = []
        if platform:
            parts.append(escape_html(str(platform)))
        if title:
            parts.append(escape_html(str(title)))
        if description:
            parts.append(escape_html(str(description)))
        text = "\n".join(parts).strip()
        if url:
            url_text = escape_html(str(url))
            text = f"{text}\n({url_text})" if text else f"({url_text})"
        if not text:
            return None
        return self._limit_caption(text)

    def _build_url_markup(self, url: str | None):
        if not url:
            return None
        return self.keyboard([[Url(self.strings("open_url"), url)]])

    def _inl_article(
        self,
        article_id: str,
        *,
        title: str,
        message_text: str,
        description: str | None = None,
    ) -> types.inline_query_result_article.InlineQueryResultArticle:
        return types.inline_query_result_article.InlineQueryResultArticle(
            id=article_id,
            title=title,
            description=description or title,
            input_message_content=types.input_text_message_content.InputTextMessageContent(
                message_text=message_text
            ),
        )

    def _inl_cached_video(
        self,
        result_id: str,
        *,
        file_id: str,
        title: str,
        caption: str | None,
        markup: types.InlineKeyboardMarkup | None,
    ) -> types.inline_query_result_cached_video.InlineQueryResultCachedVideo:
        return types.inline_query_result_cached_video.InlineQueryResultCachedVideo(
            id=result_id,
            video_file_id=file_id,
            title=title,
            caption=caption,
            reply_markup=markup,
        )

    def _inl_cached_document(
        self,
        result_id: str,
        *,
        file_id: str,
        title: str,
        caption: str | None,
        markup: types.InlineKeyboardMarkup | None,
    ) -> types.inline_query_result_cached_document.InlineQueryResultCachedDocument:
        return (
            types.inline_query_result_cached_document.InlineQueryResultCachedDocument(
                id=result_id,
                document_file_id=file_id,
                title=title,
                caption=caption,
                reply_markup=markup,
            )
        )

    def _inl_max_bytes(self) -> int:
        return self._limit_bytes(inline=True)

    def _inl_too_large_article(
        self, article_id: str
    ) -> types.inline_query_result_article.InlineQueryResultArticle:
        return self._inl_article(
            article_id,
            title=self.strings("too_large"),
            description=self.strings("inline_too_large"),
            message_text=self.strings("inline_too_large"),
        )

    def _inl_cached_media(
        self,
        *,
        result_id: str,
        file_id: str,
        title: str,
        caption: str | None,
        markup: types.InlineKeyboardMarkup | None,
        is_video: bool,
    ) -> types.InlineQueryResult:
        if is_video:
            return self._inl_cached_video(
                result_id,
                file_id=file_id,
                title=title,
                caption=caption,
                markup=markup,
            )
        return self._inl_cached_document(
            result_id,
            file_id=file_id,
            title=title,
            caption=caption,
            markup=markup,
        )

    def _inl_cached_result(
        self,
        item: dict[str, Any],
        *,
        result_id: str,
        fallback_title: str = "file",
    ) -> (
        types.inline_query_result_cached_video.InlineQueryResultCachedVideo
        | types.inline_query_result_cached_document.InlineQueryResultCachedDocument
        | None
    ):
        file_id = self._cached_file_id(item)
        if not file_id:
            return None
        title = item.get("title") or item.get("name") or fallback_title
        caption = self._build_caption(
            None,
            url=item.get("url"),
            title=title,
            description=item.get("description"),
            platform=item.get("platform"),
        ) or escape_html(title)
        markup = self._build_url_markup(item.get("url"))
        return self._inl_cached_media(
            result_id=result_id,
            file_id=file_id,
            title=title,
            caption=caption,
            markup=markup,
            is_video=self._cached_is_video(item),
        )

    def _inl_result_from_message(
        self,
        *,
        message: types.Message,
        result_id: str,
        title: str | None,
        fallback_name: str,
        url: str | None,
        info: dict[str, Any] | None,
    ) -> types.InlineQueryResult:
        caption = self._build_caption(info, url) or escape_html(title or fallback_name)
        markup = self._build_url_markup(url)
        if message.video:
            file_id = message.video.file_id
            is_video = True
        else:
            file_id = message.document.file_id if message.document else None
            if not file_id:
                raise RuntimeError("No file_id")
            is_video = False
        return self._inl_cached_media(
            result_id=result_id,
            file_id=file_id,
            title=title or fallback_name,
            caption=caption,
            markup=markup,
            is_video=is_video,
        )

    async def _inl_answer(
        self,
        inline_query: types.InlineQuery,
        results: list[types.InlineQueryResult],
    ):
        return await self.answer(
            inline_query,
            inline_results=results,
            inline_is_personal=True,
            inline_cache_time=0,
        )

    async def _inl_answer_cache_list(self, inline_query: types.InlineQuery):
        cached = self._cache_list_videos()
        if not cached:
            article = self._inl_article(
                "cache_empty",
                title=self.strings("cache_empty"),
                message_text=self.strings("cache_empty"),
            )
            return await self._inl_answer(inline_query, [article])

        max_bytes = self._inl_max_bytes()
        results: list[types.InlineQueryResult] = []
        for idx, item in enumerate(cached[:50], start=1):
            size = item.get("size")
            if isinstance(size, int) and size > max_bytes:
                article = self._inl_too_large_article(f"too_large_{idx}")
                results.append(article)
                continue
            result = self._inl_cached_result(item, result_id=f"cached_{idx}")
            if result:
                results.append(result)
        if not results:
            article = self._inl_article(
                "cache_empty",
                title=self.strings("cache_empty"),
                message_text=self.strings("cache_empty"),
            )
            return await self._inl_answer(inline_query, [article])
        return await self._inl_answer(inline_query, results)

    async def _inl_answer_cached_url(
        self,
        inline_query: types.InlineQuery,
        cached: dict[str, Any],
    ):
        max_bytes = self._inl_max_bytes()
        size = cached.get("size")
        if isinstance(size, int) and size > max_bytes:
            article = self._inl_too_large_article("too_large")
            return await self._inl_answer(inline_query, [article])

        result = self._inl_cached_result(cached, result_id="cached")
        if not result:
            return None
        return await self._inl_answer(inline_query, [result])

    async def _inl_download_url(
        self,
        inline_query: types.InlineQuery,
        url: str,
    ):
        path: Path | None = None
        try:
            result = await self._download(url, inline=True, progress=None)
            path = result.path

            try:
                message = await self._send_path(
                    chat_id=inline_query.from_user.id,
                    path=path,
                    is_video=self._is_video_info(result.info),
                    info=result.info,
                    url=url,
                )
            except TelegramForbiddenError:
                article = self._inl_article(
                    "open_pm",
                    title=self.strings("open_pm"),
                    message_text=self.strings("open_pm"),
                )
                return await self._inl_answer(inline_query, [article])
            self._store_cache(
                url, message, title=result.title, path=path, info=result.info
            )
            await self._archive_to_topic(
                message, url=url, info=result.info, title=result.title
            )
            result = self._inl_result_from_message(
                message=message,
                result_id="1",
                title=result.title,
                fallback_name=path.name,
                url=url,
                info=result.info,
            )
            return await self._inl_answer(inline_query, [result])
        except DownloadTooLarge:
            article = self._inl_too_large_article("too_large")
            return await self._inl_answer(inline_query, [article])
        except Exception:
            logger.exception("Download failed (inline). url=%s", url)
            article = self._inl_article(
                "failed",
                title=self.strings("download_failed"),
                description=self.strings("download_failed"),
                message_text=self.strings("download_failed"),
            )
            return await self._inl_answer(inline_query, [article])
        finally:
            if path and path.exists():
                try:
                    os.remove(path)
                except Exception:
                    pass

    def _cached_file_id(self, cached: dict[str, Any]) -> str | None:
        file_id = cached.get("file_id")
        return file_id if isinstance(file_id, str) and file_id else None

    def _cached_is_video(self, cached: dict[str, Any]) -> bool:
        return cached.get("type") == "video"

    async def _send_cached(self, message: types.Message, cached: dict[str, Any]):
        file_id = self._cached_file_id(cached)
        if not file_id:
            raise TelegramBadRequest(method=None, message="Missing file_id")  # type: ignore[arg-type]
        url = cached.get("url")
        caption = self._build_caption(
            None,
            url=url,
            title=cached.get("title") or cached.get("name"),
            description=cached.get("description"),
            platform=cached.get("platform"),
        )
        markup = self._build_url_markup(url)
        send_kwargs = self._message_send_kwargs(message)
        assert self.bot is not None
        if self._cached_is_video(cached):
            return await self.bot.send_video(
                chat_id=message.chat.id,
                video=file_id,
                caption=caption,
                reply_markup=markup,
                supports_streaming=True,
                **send_kwargs,
            )
        return await self.bot.send_document(
            chat_id=message.chat.id,
            document=file_id,
            caption=caption,
            reply_markup=markup,
            **send_kwargs,
        )

    def _store_cache(
        self,
        url: str,
        sent: types.Message,
        *,
        title: str | None,
        path: Path | None,
        info: dict[str, Any] | None = None,
    ) -> None:
        file = None
        media_type = "document"
        if sent.video:
            file = sent.video
            media_type = "video"
        elif sent.document:
            file = sent.document
            media_type = "document"
        if not file:
            return
        size = file.file_size
        if size is None and path:
            try:
                size = path.stat().st_size
            except Exception:
                size = None
        payload = {
            "file_id": file.file_id,
            "file_unique_id": file.file_unique_id,
            "type": media_type,
            "title": title,
            "name": path.name if path else None,
            "size": size,
            "platform": self._extract_platform(info),
            "description": info.get("description") if info else None,
        }
        self._cache_set(url, payload)

    def _message_url(self, message: types.Message) -> str | None:
        raw = self.get_args(message, raw=True)
        url = self._extract_url(raw)
        if url:
            return url
        reply = message.reply_to_message
        if reply:
            url = self._extract_url(getattr(reply, "text", None))
            if url:
                return url
            return self._extract_url(getattr(reply, "caption", None))
        return None

    def _info_filesize(self, info: dict[str, Any]) -> int | None:
        for key in ("filesize", "filesize_approx"):
            size = info.get(key)
            if isinstance(size, int) and size > 0:
                return size
        requested = info.get("requested_formats") or []
        total = 0
        for fmt in requested:
            size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
            total += int(size or 0)
        return total or None

    def _pick_info(self, info: dict[str, Any]) -> dict[str, Any]:
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            for entry in entries:
                if entry:
                    return entry
        return info

    async def _probe_ytdlp(self, url: str) -> dict[str, Any] | None:
        def _run():
            cookiefile = self._cfg_str("cookies_file")
            cookiesfrombrowser = self._cfg_str("cookies_from_browser")
            opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
            }
            if cookiefile:
                opts["cookiefile"] = cookiefile
            if cookiesfrombrowser:
                opts["cookiesfrombrowser"] = cookiesfrombrowser
            with YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.to_thread(_run)
        except Exception:
            return None
        return self._pick_info(info) if info else None

    async def _download_ytdlp(
        self,
        url: str,
        max_bytes: int | None,
        progress: ProgressState | None = None,
        title: str | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        def _run():
            def hook(data):
                downloaded = data.get("downloaded_bytes") or 0
                if max_bytes and downloaded and downloaded > max_bytes:
                    raise DownloadTooLarge()
                if progress and data.get("status") == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate")
                    progress.update_threadsafe(
                        {
                            "title": title or data.get("info_dict", {}).get("title"),
                            "downloaded": downloaded or None,
                            "total": total or None,
                            "speed": data.get("speed"),
                            "eta": data.get("eta"),
                        }
                    )

            opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
                "outtmpl": str(self.download_dir / "%(title).120s-%(id)s.%(ext)s"),
                "progress_hooks": [hook],
            }
            cookiefile = self._cfg_str("cookies_file")
            cookiesfrombrowser = self._cfg_str("cookies_from_browser")
            if cookiefile:
                opts["cookiefile"] = cookiefile
            if cookiesfrombrowser:
                opts["cookiesfrombrowser"] = cookiesfrombrowser
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                info = self._pick_info(info)
                # ``prepare_filename`` returns the *pre-merge* path —
                # for ``bestvideo+bestaudio`` formats the actual file
                # on disk has the merged container's extension (mp4
                # here), and yt-dlp records that final path in
                # ``info["filepath"]`` (or per-format
                # ``requested_downloads[i]["filepath"]``). Trust those
                # first; fall back to ``prepare_filename`` only if
                # neither is set.
                filename: str | None = info.get("filepath")
                if not filename:
                    for entry in info.get("requested_downloads") or []:
                        fp = entry.get("filepath")
                        if fp:
                            filename = fp
                            break
                if not filename:
                    filename = ydl.prepare_filename(info)
                resolved = Path(filename)
                # Final safety net — if the resolved file doesn't exist
                # on disk (e.g. yt-dlp version where ``filepath`` is
                # stale), look for any sibling that shares the stem.
                if not resolved.exists():
                    for sibling in resolved.parent.glob(resolved.stem + ".*"):
                        if sibling.is_file():
                            resolved = sibling
                            break
                return resolved, info

        return await asyncio.to_thread(_run)

    async def _download_direct(
        self,
        url: str,
        max_bytes: int | None,
        progress: ProgressState | None = None,
    ) -> tuple[Path, int | None]:
        filename = url.split("?")[0].rstrip("/").split("/")[-1] or "file"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()

                ctype = (
                    (response.headers.get("Content-Type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                # This fallback is for *direct file* links only. A page
                # URL whose yt-dlp probe failed (Instagram without
                # cookies, rate-limited YouTube, ...) lands here too —
                # downloading its HTML and shipping it as an
                # extensionless "file" is never what the user wanted.
                if ctype in ("text/html", "application/xhtml+xml"):
                    raise RuntimeError(
                        f"{url} returned a web page, not a file "
                        "(yt-dlp probe failed — for Instagram try "
                        "setting cookies_file/cookies_from_browser in config)"
                    )

                # CDN links often have no extension in the path
                # (".../v/t50.2886-16/AbCdEf"); Telegram then renders
                # the upload as a plain file. Derive the extension from
                # the Content-Type so videos arrive as videos.
                if "." not in filename and ctype:
                    ext = mimetypes.guess_extension(ctype)
                    if ext:
                        filename += ext

                target = self.download_dir / filename
                total = int(response.headers.get("Content-Length", 0) or 0)
                size = 0
                start = time.monotonic()
                last_report = 0.0
                with open(target, "wb") as f:
                    async for chunk in response.content.iter_chunked(1024 * 256):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if max_bytes and size > max_bytes:
                            raise DownloadTooLarge()
                        f.write(chunk)
                        if progress:
                            now = time.monotonic()
                            if now - last_report >= 0.8:
                                elapsed = max(now - start, 0.001)
                                speed = size / elapsed
                                eta = None
                                if total and speed > 0:
                                    eta = int((total - size) / speed)
                                progress.update(
                                    {
                                        "title": filename,
                                        "downloaded": size,
                                        "total": total or None,
                                        "speed": speed,
                                        "eta": eta,
                                    }
                                )
                                last_report = now
        return target, total or size

    async def _download(
        self,
        url: str,
        *,
        inline: bool,
        progress: ProgressState | None = None,
    ) -> DownloadResult:
        max_bytes = self._limit_bytes(inline=inline)

        info = await self._probe_ytdlp(url)
        if info:
            size = self._info_filesize(info)
            if progress:
                progress.update(
                    {
                        "title": info.get("title") or url,
                        "downloaded": 0,
                        "total": size or None,
                        "speed": None,
                        "eta": None,
                    }
                )
            if inline and size and size > max_bytes:
                raise DownloadTooLarge()
            path, info = await self._download_ytdlp(
                url,
                max_bytes if inline else None,
                progress=progress,
                title=info.get("title"),
            )
            return DownloadResult(
                path=path,
                info=info,
                title=info.get("title"),
                size=size,
            )

        if self._is_tiktok_url(url):
            raise RuntimeError("TikTok probe failed.")

        # fallback: simple file
        path, size = await self._download_direct(
            url, max_bytes if inline else None, progress=progress
        )
        if inline and size and size > max_bytes:
            raise DownloadTooLarge()
        return DownloadResult(
            path=path,
            info=None,
            title=path.name,
            size=size,
        )

    def _telegram_filename(
        self,
        path: Path,
        *,
        is_video: bool,
        info: dict[str, Any] | None = None,
    ) -> str:
        """Pick the filename Telegram sees on the upload.

        yt-dlp can leave the merged file with a non-standard or empty
        suffix (some YouTube merges produce ``<id>.``). When sending
        as video, Telegram needs a real container extension or it
        rejects the upload with a generic ``BAD_REQUEST`` and we fall
        through to ``send_document`` — that's how a YouTube video ends
        up arriving as a file with no extension.

        Strategy: keep the on-disk name when it already has a
        recognised video extension; otherwise force the extension
        derived from yt-dlp's ``info`` (defaulting to ``mp4``).
        """
        suffix = path.suffix.lower()
        if is_video and suffix not in VIDEO_EXTS:
            target_ext = "mp4"
            if info:
                ext = info.get("ext")
                if isinstance(ext, str) and ext:
                    target_ext = ext.lstrip(".").lower()
            stem = path.stem or "video"
            return f"{stem}.{target_ext}"
        return path.name

    async def _send_path(
        self,
        *,
        chat_id: int,
        path: Path,
        send_kwargs: dict[str, Any] | None = None,
        caption: str | None = None,
        is_video: bool | None = None,
        info: dict[str, Any] | None = None,
        url: str | None = None,
        prefer_video: bool = True,
    ):
        send_kwargs = send_kwargs or {}
        if caption is None:
            caption = self._build_caption(info, url)
        markup = self._build_url_markup(url)
        if is_video is None:
            is_video = self._is_video_file(path)

        # Telegram looks at the filename's extension to decide how to
        # render an upload — pin a container ext for video sends so
        # ``send_video`` doesn't fall through to ``send_document``.
        tg_filename = self._telegram_filename(path, is_video=bool(is_video), info=info)

        assert self.bot is not None
        if prefer_video and is_video:
            try:
                return await self.bot.send_video(
                    chat_id=chat_id,
                    video=FSInputFile(path, filename=tg_filename),
                    caption=caption,
                    reply_markup=markup,
                    supports_streaming=True,
                    **send_kwargs,
                )
            except TelegramBadRequest:
                pass
        return await self.bot.send_document(
            chat_id=chat_id,
            document=FSInputFile(path, filename=tg_filename),
            caption=caption,
            reply_markup=markup,
            **send_kwargs,
        )

    def _message_send_kwargs(self, message: types.Message) -> dict[str, Any]:
        return {
            "business_connection_id": getattr(message, "business_connection_id", None),
            "direct_messages_topic_id": getattr(
                message, "direct_messages_topic_id", None
            ),
        }

    async def _archive_to_topic(
        self,
        sent: Any,
        *,
        url: str | None,
        info: dict[str, Any] | None,
        title: str | None,
    ) -> None:
        """Mirror a freshly-downloaded file into the Downloader topic.

        ``sent`` is the message returned by :meth:`_send_path` — its
        ``file_id`` is reused so the mirror costs one Telegram API
        call, not another upload. Failures are logged and swallowed:
        archiving is best-effort, never user-visible.
        """
        if self.bot is None or not isinstance(sent, types.Message):
            return
        if sent.video:
            file_id = sent.video.file_id
            is_video = True
        elif sent.document:
            file_id = sent.document.file_id
            is_video = False
        else:
            return

        owner_id = self.get_user_me_id()
        if not owner_id:
            return

        try:
            chat_id, topic_id = await TopicRegistry.resolve_destination(
                DOWNLOADER_TOPIC, owner_id=owner_id
            )
        except Exception:
            logger.exception("Failed to resolve Downloader topic")
            return

        caption = self._build_caption(info, url, title=title)
        markup = self._build_url_markup(url)
        kwargs: dict[str, Any] = {"chat_id": chat_id, "caption": caption}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        if markup is not None:
            kwargs["reply_markup"] = markup

        try:
            if is_video:
                await self.bot.send_video(
                    video=file_id, supports_streaming=True, **kwargs
                )
            else:
                await self.bot.send_document(document=file_id, **kwargs)
        except Exception:
            logger.exception("Archive to Downloader topic failed")

    @command(aliases=["dl", "download"])
    async def _cmd_dl(self, message: types.Message) -> None:
        """
        {url} - download media/file
        """
        url = self._message_url(message)
        if not url:
            videos = self._cache_list_videos()
            if not videos:
                await self.answer(message, self.strings("cache_empty"))
                return
            lines = [self.strings("cache_list_title")]
            for item in videos[:20]:
                lines.append(self._format_cache_item(item))
            if len(videos) > 20:
                lines.append(f"... +{len(videos) - 20}")
            await self.answer(message, "\n".join(lines))
            return

        cached = self._cache_get(url)
        if cached:
            try:
                await self._send_cached(message, cached)
                return
            except TelegramBadRequest:
                pass

        status = await self.answer(message, self.strings("downloading"))
        if not isinstance(status, types.Message):
            return

        loop = asyncio.get_running_loop()
        progress_state = ProgressState(loop)
        progress_task = asyncio.create_task(
            self._progress_worker(status, progress_state)
        )
        path: Path | None = None
        try:
            result = await self._download(url, inline=False, progress=progress_state)
            path = result.path

            max_bytes = self._limit_bytes(inline=False)
            if path.stat().st_size > max_bytes:
                await self._stop_progress(progress_state, progress_task)
                await status.edit_text(self.strings("too_large"))
                return

            await self._stop_progress(progress_state, progress_task)
            await status.edit_text(self.strings("sending"))
            sent = await self._send_path(
                chat_id=message.chat.id,
                path=path,
                send_kwargs=self._message_send_kwargs(message),
                is_video=self._is_video_info(result.info),
                info=result.info,
                url=url,
            )
            self._store_cache(
                url, sent, title=result.title, path=path, info=result.info
            )
            await self._archive_to_topic(
                sent, url=url, info=result.info, title=result.title
            )
            await self.delete_message(status)
        except DownloadTooLarge:
            await self._stop_progress(progress_state, progress_task)
            await status.edit_text(self.strings("too_large"))
        except TelegramForbiddenError:
            await self._stop_progress(progress_state, progress_task)
        except Exception:
            logger.exception("Download failed (command). url=%s", url)
            await self._stop_progress(progress_state, progress_task)
            await status.edit_text(self.strings("download_failed"))
        finally:
            if path and path.exists():
                try:
                    os.remove(path)
                except Exception:
                    pass

    @command(aliases=["dlclearcache", "dlcacheclear", "dlcache"])
    async def _cmd_dlclearcache(self, message: types.Message) -> None:
        """
        - clear downloader cache
        """
        self.mdb.set("downloads", {})
        await self.answer(message, self.strings("cache_cleared"))

    @inline_command(aliases=["dl", "download"])
    async def _inlinecmd_dl(self, inline_query: types.InlineQuery):
        """
        {url} - download media/file
        """
        query = getattr(inline_query, "query", "") or ""
        url = self._extract_url(query)
        if not url:
            return await self._inl_answer_cache_list(inline_query)

        cached = self._cache_get(url)
        if cached:
            response = await self._inl_answer_cached_url(inline_query, cached)
            if response is not None:
                return response

        return await self._inl_download_url(inline_query, url)
