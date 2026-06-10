# description: Whisper secret messages to a specific user via inline mode
# author: @xdesai, ported to Tensai

"""SecretMessage — inline "whisper" for one specific reader.

``@bot whisper <id|@username> <text>`` posts a card into any chat; the
text itself is revealed (as a popup alert) only to the addressed user
when they tap «Open». The sender can always re-read their own whisper;
the recipient gets exactly one read — the second tap reports the
message as eaten. Anyone else gets "not for you".

Secrets are stored in the module's mdb (not in ``callback_data``), so
whispers keep working after a bot restart and the text never leaves the
server until the right person taps the button.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiogram import types

from tensai.decorators import callback_query, inline_command
from tensai.loader import Module
from tensai.utils.entity import escape_html
from tensai.utils.inline_button import build_inline_button

logger = logging.getLogger(__name__)

_MDB_KEY = "whispers"
_MAX_STORED = 100


class SecretMessage(Module):
    """
    en: Whisper secret messages to a specific user via inline mode
    ru: «Прошёптывание» секретных сообщений конкретному пользователю через инлайн
    """

    strings = {
        "ru": {
            "for_user_message": (
                "🔐 Секретное сообщение для "
                "<b><a href='tg://user?id={id}'>{name}</a></b>"
            ),
            "open": "👀 Открыть",
            "secret_message": "Секретное сообщение",
            "no_user_or_message": "Укажите пользователя и сообщение",
            "send_message": "Отправить секретное сообщение для {name}",
            "help_message": (
                "<b>Использование:</b>\n"
                "<code>@{bot} whisper (id/username) (текст)</code>"
            ),
            "cant_resolve": "Не удалось найти пользователя {target}",
            "not_for_you": "❌ Не для тебя",
            "eaten": "😽 Ты уже читал это — сообщение съели коты",
            "expired": "🕸 Сообщение не найдено (устарело)",
        },
        "en": {
            "for_user_message": (
                "🔐 Secret message for "
                "<b><a href='tg://user?id={id}'>{name}</a></b>"
            ),
            "open": "👀 Open",
            "secret_message": "Secret message",
            "no_user_or_message": "Specify the user and the message",
            "send_message": "Send a secret message for {name}",
            "help_message": (
                "<b>Usage:</b>\n"
                "<code>@{bot} whisper (id/username) (text)</code>"
            ),
            "cant_resolve": "Could not find user {target}",
            "not_for_you": "❌ Not for you",
            "eaten": "😽 You already read this — cats ate the message",
            "expired": "🕸 Message not found (expired)",
        },
    }

    # ── storage ────────────────────────────────────────────────────────────

    def _whispers(self) -> dict[str, Any]:
        raw = self.mdb.get(_MDB_KEY) or {}
        return raw if isinstance(raw, dict) else {}

    def _save_whisper(self, key: str, record: dict[str, Any]) -> None:
        whispers = self._whispers()
        whispers[key] = record
        # Keep the store bounded — drop the oldest entries first.
        while len(whispers) > _MAX_STORED:
            whispers.pop(next(iter(whispers)), None)
        self.mdb.set(_MDB_KEY, whispers)

    def _mark_opened(self, key: str) -> None:
        whispers = self._whispers()
        if key in whispers:
            whispers[key]["opened"] = True
            self.mdb.set(_MDB_KEY, whispers)

    # ── target resolution ──────────────────────────────────────────────────

    async def _resolve_target(self, token: str) -> tuple[int, str] | None:
        """``id`` or ``@username`` → ``(user_id, display_name)``."""
        token = token.strip()
        if token.lstrip("-").isdigit():
            user_id = int(token)
            name = str(user_id)
            # Best effort: a nicer name when the bot can see the chat.
            try:
                if self.bot is not None:
                    chat = await self.bot.get_chat(user_id)
                    name = chat.first_name or chat.title or name
            except Exception:
                pass
            return user_id, name

        username = token if token.startswith("@") else f"@{token}"
        try:
            if self.bot is None:
                return None
            chat = await self.bot.get_chat(username)
        except Exception:
            return None
        return chat.id, (chat.first_name or chat.title or username)

    # ── inline command ─────────────────────────────────────────────────────

    @inline_command(
        aliases=["whisper", "wsp"],
        description={
            "ru": "(id/username) (текст) — секретное сообщение для пользователя",
            "en": "(id/username) (text) — secret message for a user",
        },
    )
    async def _inlinecmd_whisper(self, query: types.InlineQuery) -> None:
        parts = (query.query or "").split(maxsplit=2)

        if len(parts) < 3:
            await self.inline_articles(
                query,
                [
                    self.make_article(
                        article_id="whisper_help",
                        title=self.strings("secret_message"),
                        description=self.strings("no_user_or_message"),
                        text=self.strings("help_message").format(
                            bot=self.get_bot_username()
                        ),
                    )
                ],
            )
            return

        _cmd, target_token, text = parts

        resolved = await self._resolve_target(target_token)
        if resolved is None:
            await self.inline_articles(
                query,
                [
                    self.make_article(
                        article_id="whisper_bad_target",
                        title=self.strings("secret_message"),
                        description=self.strings("cant_resolve").format(
                            target=escape_html(target_token)
                        ),
                        text=self.strings("cant_resolve").format(
                            target=escape_html(target_token)
                        ),
                    )
                ],
            )
            return

        user_id, name = resolved

        key = uuid.uuid4().hex[:12]
        self._save_whisper(
            key,
            {"text": text, "to": user_id, "opened": False},
        )

        # Raw callback_data + @callback_query (not the closure keyboard):
        # the recipient — not the owner — must be able to tap «Open», and
        # the whisper has to survive bot restarts. The owner-gated
        # in-memory closure registry fits neither requirement.
        kb = types.InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    build_inline_button(
                        text=self.strings("open"),
                        callback_data=f"wsp:{key}",
                    )
                ]
            ]
        )

        await self.inline_articles(
            query,
            [
                self.make_article(
                    article_id=f"whisper:{key}",
                    title=self.strings("secret_message"),
                    description=self.strings("send_message").format(name=name),
                    text=self.strings("for_user_message").format(
                        id=user_id, name=escape_html(name)
                    ),
                    reply_markup=kb,
                )
            ],
        )

    # ── reveal button ──────────────────────────────────────────────────────

    @callback_query(
        data=lambda d: isinstance(d, str) and d.startswith("wsp:"),
        access="all",  # the addressed user must be able to tap it
    )
    async def _cb_open(self, callback: types.CallbackQuery) -> None:
        key = (callback.data or "").removeprefix("wsp:")
        record = self._whispers().get(key)

        if not isinstance(record, dict):
            await callback.answer(self.strings("expired"), show_alert=True)
            return

        clicker = callback.from_user.id if callback.from_user else 0

        # The sender can always re-read their own whisper.
        if clicker == self.get_user_me_id():
            await callback.answer(str(record.get("text", "")), show_alert=True)
            return

        if clicker != record.get("to"):
            await callback.answer(self.strings("not_for_you"), show_alert=True)
            return

        if record.get("opened"):
            await callback.answer(self.strings("eaten"), show_alert=True)
            return

        await callback.answer(str(record.get("text", "")), show_alert=True)
        self._mark_opened(key)
