# SAAVN_TITLE_V1: cleaned title variants in savn_resolve
"""JioSaavn fallback tier for HellAPI.

Ye file purane (dead) server pe thi lekin git me kabhi commit nahi hui thi —
main.py ka interface match karke dobara likhi gayi hai:

  note_youtube_failure()     — YouTube fail noted (circuit +1)
  note_youtube_success()     — success => counter reset
  youtube_fallback_active()  — bool, circuit open hai?
  savn_resolve(qtitle)       — song name -> dict ya None
                               {vid,title,duration,image,artist}
  savn_media_url(vid)        — vid -> CDN audio URL (proxy stream ke liye)
  _UA                        — User-Agent (/saavn/{vid} forward me use hota)
"""
import base64
import json
import os
import re
import threading
import time

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_FAIL_THRESHOLD = int(os.getenv("SAAVN_FAIL_THRESHOLD", "3"))
_FAIL_OPEN_MIN = int(os.getenv("SAAVN_FAIL_OPEN_MIN", "10"))
_DEV_API = "https://saavn.dev/api/search/songs"
_OFFICIAL_API = "https://www.jiosaavn.com/api.php"

try:
    import pyDes  # noqa
except Exception:
    pyDes = None

_lock = threading.Lock()
_fails = 0
_open_until = 0.0
_resolve_cache = {}   # q -> (meta, vid, ts)
_media_cache = {}     # vid -> CDN audio URL
CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "savn_cache.json")


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        media = d.get("media", {})
        resolve = d.get("resolve", {})
        _media_cache.update(media)
        _resolve_cache.update(resolve)
    except Exception:
        pass


def _save_cache():
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"media": _media_cache, "resolve": _resolve_cache}, f)
    except Exception:
        pass


_load_cache()


def note_youtube_failure():
    global _fails, _open_until
    with _lock:
        _fails += 1
        if _fails >= _FAIL_THRESHOLD:
            _open_until = time.time() + _FAIL_OPEN_MIN * 60
            _fails = 0


def note_youtube_success():
    global _fails, _open_until
    with _lock:
        _fails = 0
        _open_until = 0.0


def youtube_fallback_active():
    return time.time() < _open_until


def _norm(t):
    return re.sub(r"\s+", " ", str(t or "").strip())


def _clean_html(t):
    return re.sub(r"<[^>]+>", "", str(t or "")).strip().replace("&quot;", '"')


def _pick_download(urls):
    if not urls:
        return ""
    if isinstance(urls, list):
        best = ""
        for it in urls:
            link = it.get("link") or it.get("url") or ""
            if link and it.get("quality") == "320kbps":
                return link
            if link:
                best = link
        return best
    return str(urls)


def _dev_search(q):
    r = requests.get(
        _DEV_API,
        params={"query": q, "limit": 5},
        headers={"User-Agent": _UA}, timeout=12)
    r.raise_for_status()
    data = r.json()
    if data.get("success") is False:
        return None
    res = (data.get("data") or {}).get("results") or []
    if not res:
        return None
    for s in res:
        try:
            dl = s.get("downloadUrl") or s.get("download_url") or []
            link = _pick_download(dl)
            if not link:
                continue
            vid = str(s.get("id") or "")
            title = _clean_html(s.get("name") or s.get("title") or "")
            if not vid or not title:
                continue
            return {
                "vid": vid,
                "title": title,
                "duration": _int_or(s.get("duration"), 0),
                "image": s.get("image") or s.get("img") or "",
                "artist": _clean_html(
                    (s.get("artists") or {}).get("primary")
                    or s.get("primaryArtists") or "JioSaavn"),
                "link": link,
            }
        except Exception:
            continue
    return None


def _int_or(v, d=0):
    try:
        return int(float(str(v).strip()))
    except Exception:
        return d


def _decrypt_media(enc):
    if not pyDes or not enc:
        return ""
    try:
        des = pyDes.des(b"38346591", pyDes.ECB, padmode=pyDes.PAD_PKCS5)
        return des.decrypt(base64.b64decode(enc)).decode("utf-8", "ignore")
    except Exception:
        return ""


def _official_search(q):
    r = requests.get(
        _OFFICIAL_API,
        params={
            "__call": "search.getResults", "q": q, "p": 1,
            "_format": "json", "_marker": 0, "api_version": 4,
            "ctx": "web6dot0", "n": 5,
        },
        headers={"User-Agent": _UA}, timeout=12)
    r.raise_for_status()
    data = r.json()
    songs = data.get("results") or []
    if not songs:
        return None
    for s in songs:
        try:
            mi = s.get("more_info") or {}
            enc = mi.get("encrypted_media_url") or ""
            link = _decrypt_media(enc)
            if not link:
                continue
            vid = str(s.get("id") or mi.get("id") or "")
            title = _clean_html(s.get("title") or s.get("entity_name") or "")
            if not vid or not title:
                continue
            return {
                "vid": vid,
                "title": title,
                "duration": _int_or(
                    s.get("duration") or (mi.get("duration") if mi else 0), 0),
                "image": (s.get("image") or "").split("?")[0],
                "artist": _clean_html(
                    s.get("primaryArtists") or s.get("singer") or "JioSaavn"),
                "link": link,
            }
        except Exception:
            continue
    return None


def savn_resolve(qtitle):
    """Song name -> JioSaavn meta dict ya None (sync, executor me chalega)."""
    q = _norm(qtitle)
    if not q:
        return None
    now = time.time()
    with _lock:
        hit = _resolve_cache.get(q)
    if hit and now - hit[2] < 6 * 3600 and _media_cache.get(hit[1]):
        return hit[0]
    # SAAVN_TITLE_V1: pipe/dash titles clean karke variants me search karo
    variants = []
    for cand in (q, q.split("|")[0].strip(), q.split(" - ")[0].strip()):
        cand = re.sub(r"[\s\-–—:]*\b(official video|official audio|lyrical video|full video|hd video|video song)\b.*$",
                      "", cand, flags=re.I)
        cand = cand.strip(" |-–—:")
        if cand and cand.lower() not in [v.lower() for v in variants]:
            variants.append(cand)
    meta = None
    for qq in variants:
        for fn in (_dev_search, _official_search):
            try:
                meta = fn(qq)
                if meta:
                    break
            except Exception:
                continue
        if meta:
            break
    if not meta:
        return None
    link = meta.pop("link") or ""
    if not link:
        return None
    with _lock:
        _media_cache[meta["vid"]] = link
        _resolve_cache[q] = (meta, meta["vid"], now)
    _save_cache()
    return meta


def savn_media_url(vid):
    with _lock:
        return _media_cache.get(vid)
