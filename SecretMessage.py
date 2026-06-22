# description: Whisper secret messages to a specific user via inline mode
# author: @xdesai, ported to Tensai
# version: 2.0.0

"""SecretMessage — inline "whisper" for one specific reader.

``@bot whisper @<username> <text>`` posts a card into any chat; the
text is revealed (as a popup alert) only to the addressed user when
they tap «Open». The sender can always re-read their own whisper;
the recipient gets exactly one read — the second tap reports the
message as eaten. Anyone else gets "not for you".

Recipient check happens **at click time** by comparing
``callback.from_user.username`` against the stored username — no
``get_chat`` lookup, no Telegram-id resolution. That keeps the inline
flow fast (no API round-trip per whisper) and side-steps the cases
where ``bot.get_chat("@user")`` simply doesn't work (private
accounts, never-talked-to-bot, business-connection mode). It also
means the recipient *must* have a public ``@username`` to receive
whispers.

Secrets are stored in the module's mdb (not in ``callback_data``), so
whispers keep working after a bot restart and the text never leaves
the server until the right person taps the button.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiogram import types

from tensai import types as tensai_types
from tensai.decorators import callback_query, inline_command
from tensai.loader import Module
from tensai.utils.entity import escape_html
from tensai.utils.inline_button import build_inline_button

__version__ = "2.0.0"

logger = logging.getLogger(__name__)

_MDB_KEY = "whispers"
_MAX_STORED = 100


class SecretMessage(Module):
    """
    en: Whisper secret messages to a specific user via inline mode.
    ru: «Прошёптывание» секретных сообщений конкретному пользователю через инлайн.
    """

    strings = tensai_types.ModuleStrings(
        tensai_types.Translation(
            "for_user_message",
            en="🔐 Secret message for <b>@{username}</b>",
            ru="🔐 Секретное сообщение для <b>@{username}</b>",
        ),
        tensai_types.Translation(
            "open", en=":e:eyes Open", ru=":e:eyes Открыть",
        ),
        tensai_types.Translation(
            "secret_message", en="Secret message", ru="Секретное сообщение",
        ),
        tensai_types.Translation(
            "no_user_or_message",
            en="Specify @username and message text",
            ru="Укажите @username и текст сообщения",
        ),
        tensai_types.Translation(
            "send_message",
            en="Send a secret to @{username}",
            ru="Отправить секретное сообщение для @{username}",
        ),
        tensai_types.Translation(
            "help_message",
            en=(
                "<b>:e:info Usage:</b>\n"
                "<code>@{bot} whisper @username (text)</code>\n\n"
                "<i>The recipient must have a public @username.</i>"
            ),
            ru=(
                "<b>:e:info Использование:</b>\n"
                "<code>@{bot} whisper @username (текст)</code>\n\n"
                "<i>У получателя должен быть публичный @username.</i>"
            ),
        ),
        tensai_types.Translation(
            "bad_target",
            en=":e:cross Provide a @username (not a numeric id).",
            ru=":e:cross Укажи @username (не числовой id).",
        ),
        tensai_types.Translation(
            "not_for_you", en=":e:cross Not for you", ru=":e:cross Не для тебя",
        ),
        tensai_types.Translation(
            "no_username",
            en=":e:cross Set a public @username to receive whispers",
            ru=":e:cross Установи публичный @username, чтобы получать сообщения",
        ),
        tensai_types.Translation(
            "eaten",
            en="😽 You already read this — cats ate the message",
            ru="😽 Ты уже читал это — сообщение съели коты",
        ),
        tensai_types.Translation(
            "expired",
            en="🕸 Message not found (expired)",
            ru="🕸 Сообщение не найдено (устарело)",
        ),
    )

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

    # ── target parsing (no Telegram lookups) ───────────────────────────────

    @staticmethod
    def _parse_username(token: str) -> str | None:
        """Extract a lowercase, no-``@`` username from the first query word.

        Rejects numeric ids — the whole point of v2 is to drop ``get_chat``
        and verify at click time, which only works for usernames. Returns
        ``None`` on empty / digits / leading dash / anything that isn't a
        well-formed username (4–32 alnum/underscore chars).
        """
        token = token.strip().lstrip("@").lower()
        if not token or token.lstrip("-").isdigit():
            return None
        if not (4 <= len(token) <= 32) or not all(
            c.isalnum() or c == "_" for c in token
        ):
            return None
        return token

    # ── inline command ─────────────────────────────────────────────────────

    @inline_command(
        aliases=["whisper", "wsp"],
        description={
            "ru": "@username (текст) — секретное сообщение для пользователя",
            "en": "@username (text) — secret message for a user",
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
        username = self._parse_username(target_token)

        if username is None:
            await self.inline_articles(
                query,
                [
                    self.make_article(
                        article_id="whisper_bad_target",
                        title=self.strings("secret_message"),
                        description=self.strings("bad_target"),
                        text=self.strings("bad_target"),
                    )
                ],
            )
            return

        key = uuid.uuid4().hex[:12]
        self._save_whisper(
            key,
            {"text": text, "to_username": username, "opened": False},
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

        safe_username = escape_html(username)
        await self.inline_articles(
            query,
            [
                self.make_article(
                    article_id=f"whisper:{key}",
                    title=self.strings("secret_message"),
                    description=self.strings("send_message").format(
                        username=username
                    ),
                    text=self.strings("for_user_message").format(
                        username=safe_username
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

        # The sender can always re-read their own whisper.
        clicker_id = callback.from_user.id if callback.from_user else 0
        if clicker_id == self.get_user_me_id():
            await callback.answer(str(record.get("text", "")), show_alert=True)
            return

        # Username-only verification — no ``get_chat`` round-trip. The
        # recipient must have a public @username (private accounts can't
        # receive whispers in v2; this is intentional).
        clicker_username = (
            getattr(callback.from_user, "username", None) or ""
        ).lower()
        target_username = str(record.get("to_username") or "").lower()

        if not clicker_username:
            await callback.answer(self.strings("no_username"), show_alert=True)
            return

        if clicker_username != target_username:
            await callback.answer(self.strings("not_for_you"), show_alert=True)
            return

        if record.get("opened"):
            await callback.answer(self.strings("eaten"), show_alert=True)
            return

        await callback.answer(str(record.get("text", "")), show_alert=True)
        self._mark_opened(key)
