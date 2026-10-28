"""Helper script: finds chat_id values the bot can currently see.

Use this after adding/replacing a bot in a group to get the correct
group_chat_id for config.json.

Usage:
1. Stop bot.py first (only one process can poll getUpdates with the same
   token at a time; running both risks a "Conflict" error).
2. Add the bot to the target group (as a regular member is enough).
3. Send any message in that chat (e.g. "/start" or "hi") — the bot doesn't
   need to reply to it, it just needs the update to exist.
4. Run: python get_chatid.py
"""
import asyncio
import json
from pathlib import Path

from telegram import Bot

BASE_DIR = Path(__file__).parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))


async def main():
    bot = Bot(token=CONFIG["bot_token"])
    updates = await bot.get_updates(allowed_updates=["message", "channel_post", "my_chat_member"])

    if not updates:
        print(
            "Нет новых апдейтов.\n"
            "Добавьте бота в группу (или напишите ему в личку), отправьте туда "
            "любое сообщение и запустите скрипт снова."
        )
        return

    seen = {}
    for u in updates:
        chat = None
        if u.message:
            chat = u.message.chat
        elif u.channel_post:
            chat = u.channel_post.chat
        elif u.my_chat_member:
            chat = u.my_chat_member.chat
        if chat:
            seen[chat.id] = chat

    if not seen:
        print("Апдейты есть, но подходящих чатов среди них не найдено.")
        return

    print("Найденные чаты:")
    for chat_id, chat in seen.items():
        name = chat.title or chat.username or chat.first_name or ""
        print(f"  chat_id={chat_id:<16} type={chat.type:<12} name={name!r}")
    print("\nСкопируйте нужный chat_id в config.json -> group_chat_id.")


if __name__ == "__main__":
    asyncio.run(main())
