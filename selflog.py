# This file is a test module for Tensai userbot.

# description: selflog module — caches edits/deletes/self-destructs into the Selflog topic
# author: @vsecoder

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import logging
import os
from typing import Any

from aiogram import types
from aiogram.types import BusinessMessagesDeleted, FSInputFile

import tensai
import tensai.database as _db_pkg
from tensai import types as tensai_types
from tensai.decorators import (
    business_message,
    command,
    deleted_business_message,
    edited_business_message,
)
from tensai.loader import Module
from tensai.utils.topics import TopicRegistry

logger = logging.getLogger(__name__)

# Mapping: media kind on a Message → file extension used when downloading.
MEDIA_EXTS: dict[str, str] = {
    "photo": "jpg",
    "video": "mp4",
    "voice": "ogg",
    "video_note": "mp4",
}

# Three weeks — the longest Telegram retains business deletions for, so
# anything older is unrecoverable anyway.
REDIS_TTL_SECONDS = 60 * 60 * 24 * 21

SELFLOG_DOWNLOAD_DIR = os.path.join("downloads", "selflog")

# Single topic name for everything; `topic_name` doubles as the cache
# key in TopicRegistry. Hashtags inside each message keep posts
# searchable: `#u<id>` filters by author, `#sd` / `#edit` / `#deleted`
# filter by event kind, plain `#selflog` to find the whole feed.
SELFLOG_TOPIC = "Selflog"


class Selflog(Module):
    """Selflog feed for the bot owner.

    All events (self-destructed media, edits, deletes) flow into ONE
    topic named ``Selflog`` in the bot's DM with the owner. Each post
    is tagged with hashtags so Telegram's in-topic search gives a
    usable filter:

    - ``#selflog``           — entire feed
    - ``#sd`` / ``#edit`` / ``#deleted`` — filter by event kind
    - ``#u<user_id>``        — filter by author of the original message
    """

    config = tensai_types.ModuleConfig(
        tensai_types.ConfigValue(
            key="enabled",
            name="Enabled",
            default=False,
            type=tensai_types.BoolType(),
            description="Enable selflog handlers (caches edits/deletes/self-destructs).",
        ),
    )

    strings = {
        "ru": {
            "selflog_on": (
                "<b><tg-emoji emoji-id=6028565819225542441>✅</tg-emoji> Selflog включен!</b>\n\n"
                "<i>Логи летят в топик <code>Selflog</code> в ЛС с ботом. "
                "Поиск внутри топика: <code>#u&lt;id&gt;</code> — по автору, "
                "<code>#sd</code>/<code>#edit</code>/<code>#deleted</code> — по типу события.</i>"
            ),
            "selflog_off": "<b><tg-emoji emoji-id=6030331836763213973>❌</tg-emoji> Selflog выключен!</b>",
            "self_destructed_message": (
                "#selflog #sd #u{user_id}\n"
                "<b>🔥 Самоуничтожившееся сообщение</b>\n"
                '👤 <a href="tg://user?id={user_id}">{full_name}</a>{username}\n'
                "🕐 <code>{sent_at}</code>"
            ),
            "edit_message": (
                "#selflog #edit #u{user_id}\n"
                "<b>✏️ Изменено (старая версия ниже)</b>\n"
                '👤 <a href="tg://user?id={user_id}">{full_name}</a>{username}\n'
                "🕐 <code>{sent_at}</code>"
            ),
            "deleted_message": (
                "#selflog #deleted #u{user_id}\n"
                "<b>🗑 Удалено</b>\n"
                '👤 <a href="tg://user?id={user_id}">{full_name}</a>{username}\n'
                "🕐 <code>{sent_at}</code>"
            ),
            "open": "Открыть чат",
        },
        "en": {
            "selflog_on": (
                "<b><tg-emoji emoji-id=6028565819225542441>✅</tg-emoji> Selflog is on!</b>\n\n"
                "<i>Logs go to the <code>Selflog</code> topic in the bot DM. "
                "Search inside that topic: <code>#u&lt;id&gt;</code> — by author, "
                "<code>#sd</code>/<code>#edit</code>/<code>#deleted</code> — by event type.</i>"
            ),
            "selflog_off": "<b><tg-emoji emoji-id=6030331836763213973>❌</tg-emoji> Selflog is off!</b>",
            "self_destructed_message": (
                "#selflog #sd #u{user_id}\n"
                "<b>🔥 Self-destructed</b>\n"
                '👤 <a href="tg://user?id={user_id}">{full_name}</a>{username}\n'
                "🕐 <code>{sent_at}</code>"
            ),
            "edit_message": (
                "#selflog #edit #u{user_id}\n"
                "<b>✏️ Edited (original below)</b>\n"
                '👤 <a href="tg://user?id={user_id}">{full_name}</a>{username}\n'
                "🕐 <code>{sent_at}</code>"
            ),
            "deleted_message": (
                "#selflog #deleted #u{user_id}\n"
                "<b>🗑 Deleted</b>\n"
                '👤 <a href="tg://user?id={user_id}">{full_name}</a>{username}\n'
                "🕐 <code>{sent_at}</code>"
            ),
            "open": "Open chat",
        },
    }

    async def on_load(self) -> None:
        self._migrate_legacy()

    # ----- helpers -----

    def _bot(self):
        bot = tensai.bot
        if bot is None:
            raise RuntimeError("Bot is not initialised yet")
        return bot

    def _redis(self):
        return _db_pkg.redis

    def _migrate_legacy(self) -> None:
        """Drop pre-9.4 storage that's now obsolete: ``mdb.users.*`` (per-user
        topics in a separate forum chat — no longer used) and the legacy
        ``tensai.selflog.chat_id`` key (we always log to the Selflog topic
        in the bot DM now)."""
        if self.mdb.get("legacy_migrated"):
            return

        legacy_status = self.db.get("tensai.selflog.status")
        if isinstance(legacy_status, bool):
            try:
                self.config.set("enabled", legacy_status)
            except Exception:
                pass

        # Clean up old per-user topic registry — it's a dead structure now.
        self.mdb.set("users", {})

        self.mdb.set("legacy_migrated", True)

    def _is_enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    async def _resolve_dest(self) -> tuple[int, int | None] | None:
        """Resolve ``(chat_id, topic_id_or_None)`` for the Selflog topic.

        Returns ``None`` when the bot is not yet bound to an owner —
        handlers skip silently in that case (selflog should never write
        before install completes).
        """
        owner_id = self.get_user_me_id()
        if not owner_id:
            return None
        return await TopicRegistry.resolve_destination(SELFLOG_TOPIC, owner_id=owner_id)

    def _user_fields(self, user: types.User | None) -> dict[str, Any]:
        """Build the format kwargs for the per-event header strings.

        Returns a dict with ``user_id`` / ``full_name`` (HTML-escaped) /
        ``username`` (already-escaped, leading space + ``(@handle)``;
        empty when the user has no @username so the substitution
        collapses cleanly).
        """
        if user is None:
            return {"user_id": 0, "full_name": "Unknown", "username": ""}
        raw_name = (user.full_name or "").strip() or (user.username or "Unknown")
        username_part = f" (@{user.username})" if user.username else ""
        return {
            "user_id": user.id,
            "full_name": self.escape_html(raw_name),
            "username": self.escape_html(username_part),
        }

    @staticmethod
    def _format_when(when: _dt.datetime | None) -> str:
        """Render a Telegram ``Message.date`` as ``YYYY-MM-DD HH:MM UTC``.

        Telegram returns timezone-aware UTC datetimes; we still call
        ``astimezone(timezone.utc)`` defensively in case any future
        callers pass a naive datetime.
        """
        if when is None:
            return "?"
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    @staticmethod
    def _redis_key(chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    @staticmethod
    def _sd_cache_key(chat_id: int, message_id: int) -> str:
        return f"self_destructing.{chat_id}.{message_id}"

    @staticmethod
    def _safe_filename(file_id: str, kind: str, ext: str | None = None) -> str:
        ext = ext or MEDIA_EXTS.get(kind, "bin")
        safe_id = hashlib.sha1(file_id.encode("utf-8")).hexdigest()[:12]
        return f"{safe_id}.{ext}"

    def _extract_media(
        self, message: types.Message, prefix: str = ""
    ) -> tuple[str, str, str] | None:
        for kind, ext in MEDIA_EXTS.items():
            media = getattr(message, f"{prefix}{kind}", None)
            if kind == "photo" and isinstance(media, list):
                media = media[-1] if media else None
            if media:
                file_id = getattr(media, "file_id", None)
                if file_id:
                    return file_id, kind, ext
        return None

    # ----- commands -----

    @command(aliases=["selflogmode"])
    async def _cmd_selflogmode(self, message: types.Message) -> None:
        """
        - enable or disable selflog mode
        """
        new_status = not self._is_enabled()
        self.config.set("enabled", new_status)
        await self.answer(
            message,
            self.strings("selflog_on" if new_status else "selflog_off"),
        )

    # ----- handlers -----

    @business_message()
    async def _bismsg_selflog(self, message: types.Message) -> None:
        if not self._is_enabled() or message.from_user is None:
            return

        await self._set_message(message)

        admin_id = self.get_user_me_id()
        reply = message.reply_to_message
        if not reply or reply.from_user is None:
            return
        if message.from_user.id != admin_id or reply.from_user.id == admin_id:
            return

        redis = self._redis()
        if redis is None:
            return
        try:
            already_cached = await redis.get(
                self._redis_key(message.chat.id, reply.message_id)
            )
        except Exception:
            already_cached = None
        if already_cached:
            return

        dest = await self._resolve_dest()
        if dest is None:
            return
        chat_id, topic_id = dest

        await self._send_text(
            chat_id,
            topic_id,
            self.strings("self_destructed_message").format(
                **self._user_fields(reply.from_user),
                sent_at=self._format_when(reply.date),
            ),
        )

        extracted = self._extract_media(reply)
        if extracted:
            file_id, kind, _ext = extracted
            await self._send_media(chat_id, topic_id, kind, file_id, reply)

    @edited_business_message()
    async def _bisedit_selflog(self, message: types.Message) -> None:
        if not self._is_enabled():
            return

        redis = self._redis()
        if redis is None:
            return
        try:
            model_dump = await redis.get(
                self._redis_key(message.chat.id, message.message_id)
            )
        except Exception:
            return
        if not model_dump:
            return

        original_message = types.Message.model_validate_json(model_dump)
        if not original_message.from_user:
            return

        dest = await self._resolve_dest()
        if dest is None:
            return
        chat_id, topic_id = dest

        bot = self._bot()
        user_fields = self._user_fields(original_message.from_user)

        await self._send_text(
            chat_id,
            topic_id,
            self.strings("edit_message").format(
                **user_fields,
                sent_at=self._format_when(original_message.date),
            ),
        )

        copy_kwargs: dict[str, Any] = {"chat_id": chat_id}
        if topic_id is not None:
            copy_kwargs["message_thread_id"] = topic_id
        await original_message.send_copy(
            **copy_kwargs,
            reply_markup=types.InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        types.InlineKeyboardButton(
                            text=self.strings("open"),
                            url=(
                                "tg://openmessage?"
                                f"user_id={user_fields['user_id']}"
                                f"&message_id={original_message.message_id}"
                            ),
                        )
                    ]
                ]
            ),
        ).as_(bot)

    @deleted_business_message()
    async def _bisdel_selflog(self, business_messages: BusinessMessagesDeleted) -> None:
        if not self._is_enabled():
            return

        redis = self._redis()
        if redis is None:
            return

        try:
            pipe = redis.pipeline()
            for message_id in business_messages.message_ids:
                pipe.get(self._redis_key(business_messages.chat.id, message_id))
            messages_data = await pipe.execute()
        except Exception:
            return

        dest = await self._resolve_dest()
        if dest is None:
            return
        chat_id, topic_id = dest

        bot = self._bot()
        keys_to_delete: list[str] = []

        for message_id, model_dump in zip(business_messages.message_ids, messages_data):
            if not model_dump:
                continue

            original_message = types.Message.model_validate_json(model_dump)
            if not original_message.from_user:
                continue

            await self._send_text(
                chat_id,
                topic_id,
                self.strings("deleted_message").format(
                    **self._user_fields(original_message.from_user),
                    sent_at=self._format_when(original_message.date),
                ),
            )

            copy_kwargs: dict[str, Any] = {"chat_id": chat_id}
            if topic_id is not None:
                copy_kwargs["message_thread_id"] = topic_id
            await original_message.send_copy(**copy_kwargs).as_(bot)

            await asyncio.sleep(1.2)
            keys_to_delete.append(
                self._redis_key(business_messages.chat.id, message_id)
            )

        if keys_to_delete:
            try:
                await redis.delete(*keys_to_delete)
            except Exception:
                pass

    # ----- internals -----

    async def _set_message(self, message: types.Message) -> None:
        """Cache the incoming business message in redis (so we can recall
        the original on edit/delete events) and download self-destructing
        media to local disk (because Telegram revokes ``file_id`` once
        the message self-destructs)."""
        if message.from_user is None or message.from_user.id == self.get_user_me_id():
            return

        redis = self._redis()
        if redis is not None:
            try:
                await redis.set(
                    self._redis_key(message.chat.id, message.message_id),
                    message.model_dump_json(),
                    ex=REDIS_TTL_SECONDS,
                )
            except Exception:
                logger.exception("Failed to cache message in redis.")

        await self._cache_self_destructing(message)

    async def _cache_self_destructing(self, message: types.Message) -> None:
        extracted = self._extract_media(message, prefix="self_destructing_")
        if not extracted:
            return
        file_id, kind, ext = extracted
        os.makedirs(SELFLOG_DOWNLOAD_DIR, exist_ok=True)
        file_path = os.path.join(
            SELFLOG_DOWNLOAD_DIR,
            f"sd_{message.chat.id}_{message.message_id}_{self._safe_filename(file_id, kind, ext)}",
        )
        try:
            await self._bot().download(file_id, destination=file_path)
        except Exception:
            logger.warning("Failed to cache self-destructing media.")
            return
        self.mdb.set(
            self._sd_cache_key(message.chat.id, message.message_id),
            {"path": file_path, "kind": kind, "file_id": file_id},
        )

    async def _send_text(self, chat_id: int, topic_id: int | None, text: str) -> None:
        """Send a text post into the Selflog topic.

        Falls back to the chat's general thread (no ``message_thread_id``)
        if the first send fails. The cached topic id is dropped **only**
        when Telegram says the topic is gone — bad-HTML / rate-limit /
        other transient errors leave the cache intact, otherwise every
        hiccup forces a fresh topic to be created on next restart.
        """
        bot = self._bot()
        kwargs: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        try:
            await bot.send_message(**kwargs)
            return
        except Exception as exc:
            if topic_id is None:
                raise
            if TopicRegistry.is_topic_gone_error(exc):
                TopicRegistry.forget_topic(SELFLOG_TOPIC)
                logger.warning(
                    "Selflog topic %s is gone; cache cleared, will recreate.",
                    topic_id,
                )
            else:
                logger.warning(
                    "Selflog send_message failed in topic %s (%s); "
                    "retrying in general thread, keeping cache.",
                    topic_id,
                    exc,
                )

        await bot.send_message(chat_id=chat_id, text=text)

    async def _send_media_kind(
        self,
        bot: Any,
        chat_id: int,
        topic_id: int | None,
        kind: str,
        media: Any,
    ) -> None:
        """Dispatch the right ``send_*`` based on media kind. Centralises
        photo/video/voice/video_note branches that previously lived in
        two places."""
        kwargs: dict[str, Any] = {"chat_id": chat_id}
        if topic_id is not None:
            kwargs["message_thread_id"] = topic_id
        if kind == "photo":
            await bot.send_photo(photo=media, **kwargs)
        elif kind == "video":
            await bot.send_video(video=media, **kwargs)
        elif kind == "video_note":
            await bot.send_video_note(video_note=media, **kwargs)
        elif kind == "voice":
            await bot.send_voice(voice=media, **kwargs)

    async def _send_media(
        self,
        chat_id: int,
        topic_id: int | None,
        kind: str,
        file_id: str,
        reply: types.Message | None = None,
    ) -> None:
        bot = self._bot()

        if reply and await self._send_cached_self_destructing(chat_id, topic_id, reply):
            return

        try:
            await self._send_media_kind(bot, chat_id, topic_id, kind, file_id)
            return
        except Exception:
            logger.exception("Failed to send cached file_id, falling back to download.")

        os.makedirs(SELFLOG_DOWNLOAD_DIR, exist_ok=True)
        file_path = os.path.join(
            SELFLOG_DOWNLOAD_DIR, self._safe_filename(file_id, kind)
        )
        try:
            await bot.download(file_id, destination=file_path)
        except FileNotFoundError:
            logger.warning("Selflog media file not found: %s", file_id)
            return
        except OSError:
            logger.exception("Failed to download selflog media: %s", file_id)
            return

        try:
            await self._send_media_kind(
                bot, chat_id, topic_id, kind, FSInputFile(file_path)
            )
        finally:
            await asyncio.sleep(1.2)
            try:
                os.remove(file_path)
            except Exception:
                pass

    async def _send_cached_self_destructing(
        self,
        chat_id: int,
        topic_id: int | None,
        reply: types.Message,
    ) -> bool:
        cached = self.mdb.get(self._sd_cache_key(reply.chat.id, reply.message_id))
        if not isinstance(cached, dict):
            return False
        path = cached.get("path")
        kind = cached.get("kind")
        if not path or not kind or not os.path.exists(path):
            return False
        await self._send_media_kind(
            self._bot(), chat_id, topic_id, kind, FSInputFile(path)
        )
        return True
