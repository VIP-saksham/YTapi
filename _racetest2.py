
import time, ytdlp
alive = ytdlp._alive_proxies()
print("ALIVE", alive)
for vid in ("papuvlVeZg8", "JGwWNGJdvx8"):
    t = time.time()
    try:
        info, p = ytdlp._extract_race(
            f"https://www.youtube.com/watch?v={vid}",
            "bestaudio[ext=m4a]/bestaudio/best", alive)
        print("OK", vid, int((time.time()-t)*1000), "ms winner=", p)
    except Exception as e:
        print("FAIL", vid, int((time.time()-t)*1000), "ms", str(e)[:70])
print("HEALTH:")
for row in ytdlp.get_proxy_health():
    print(" ", row)
