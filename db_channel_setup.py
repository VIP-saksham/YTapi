"""
db_channel_setup.py — HellAPI ka DB-channel auto-setup

Kya karta hai:
  1. Bot ko channel pe POST dekhne deta hai (getUpdates polling)
  2. Tum channel me koi bhi message bhejo (bot admin hone chahiye)
     → script channel ID pakad leti hai
  3. .env me DB_CHANNEL_ID automatically add/update kar deti hai
  4. Test message bhej ke verify karti hai

Kaise chalao:
  ./venv/bin/python db_channel_setup.py

Steps (Telegram me):
  1. Telegram → New Channel banao (private, naam kuch bhi — "HellAPI DB")
  2. Channel me @youtubeapiiiibot ko ADMIN banao (Post Messages ON)
  3. Channel me ek message bhejo ("test" type kar do)
  4. Ye script chala do — ID auto save ho jayegi
"""

import os
import sys
import time
import asyncio

sys.stdout.reconfigure(encoding="utf-8")


async def main():
    from telegram import Bot
    from dotenv import load_dotenv
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN .env me nahi hai")
        return

    bot = Bot(token)
    await bot.initialize()
    me = await bot.get_me()
    print(f"🤖 Bot: @{me.username}")
    print("\n📌 Steps:")
    print("   1. Naya private channel banao (e.g. 'HellAPI DB')")
    print(f"   2. @{me.username} ko us channel me ADMIN banao (Post Messages ON)")
    print("   3. Channel me ek message bhejo (kuch bhi — 'test')")
    print("   4. Ye script chalti rahegi, ID khud pakad legi...\n")

    print("⏳ Listening for channel posts (60 sec)...")
    found = None
    deadline = time.time() + 60
    offset = None
    while time.time() < deadline and not found:
        try:
            updates = await bot.get_updates(
                offset=offset, timeout=25,
                allowed_updates=["channel_post", "message", "my_chat_member"],
            )
        except Exception as e:
            print(f"   poll error: {e}")
            await asyncio.sleep(2)
            continue
        for u in updates:
            offset = u.update_id + 1
            post = getattr(u, "channel_post", None)
            if post and post.chat and str(post.chat.id).startswith("-100"):
                found = post.chat
                break
            mcm = getattr(u, "my_chat_member", None)
            if mcm and mcm.chat and str(mcm.chat.id).startswith("-100"):
                found = mcm.chat
                break
        if not found:
            print("   ... abhi kuch nahi aaya (channel me message bhejo)")

    if not found:
        print("\n❌ 60 sec me channel nahi mila. Steps follow kiye? "
              "Bot ko admin banane ke BAAD message bhejo.")
        await bot.shutdown()
        return

    print(f"\n✅ Channel found: {found.id} ({found.title or found.username})")

    # Permission check + test message
    try:
        member = await bot.get_chat_member(found.id, me.id)
        can_post = getattr(member, "can_post_messages", False)
        print(f"   Admin: yes | can_post_messages: {can_post}")
        msg = await bot.send_message(
            found.id,
            "🛠 HellAPI DB store test — ye message cache database hai. "
            "Delete mat karna!")
        print(f"✅ Test message sent: msg_id={msg.message_id}")
    except Exception as e:
        print(f"⚠ Message send fail (bot admin hai?): {e}")

    # .env me save
    env_path = ".env"
    lines = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    new_line = f"DB_CHANNEL_ID={found.id}"
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("DB_CHANNEL_ID="):
            lines[i] = new_line
            replaced = True
    if not replaced:
        lines.append(new_line)
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"💾 .env me save ho gaya: {new_line}")
    print("\n🚀 Ab API restart karo:  systemctl restart hellapi-api  (ya manual)")

    await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
