#!/usr/bin/env python3
# _purge.py — audio/video mix-up era ka stale stream-cache clear
import sqlite3

c = sqlite3.connect("/root/hellapi/bot_data.db")
n = c.execute("""
    UPDATE video_cache
    SET stream_url='', stream_expires_at=0, stream_format='',
        format_id='', ext='', resolution=''
    WHERE stream_url IS NOT NULL AND stream_url != ''
""").rowcount
c.commit()
print("stream-cache purged rows:", n)
print("remaining non-empty:", c.execute(
    "SELECT COUNT(*) FROM video_cache WHERE stream_url != ''").fetchone()[0])
