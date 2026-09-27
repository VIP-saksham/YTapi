
import time, ytdlp
alive = ytdlp._alive_proxies()
print("ALIVE", alive)
t = time.time()
try:
    info, p = ytdlp._extract_race(
        "https://www.youtube.com/watch?v=Kfa2vq2jQHM",
        "bestaudio[ext=m4a]/bestaudio/best", alive)
    print("OK", int((time.time()-t)*1000), "ms winner=", p)
except Exception as e:
    print("FAIL", int((time.time()-t)*1000), "ms", str(e)[:90])
print("HEALTH:")
for row in ytdlp.get_proxy_health():
    print(" ", row)
