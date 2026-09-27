"""Chrome DOM se visitorData nikal ke /root/hellapi/.visitor_data me likho."""
import re
import sys

html = open("/tmp/yt_dom.html", encoding="utf-8", errors="ignore").read()

patterns = [
    r'"visitorData":"([^"]+)"',                 # ytInitialData JSON
    r'visitorData%22%3A%22([^%&]+)%22',         # URL-encoded variants
]
vd = None
for p in patterns:
    m = re.search(p, html)
    if m:
        vd = m.group(1)
        break

if vd:
    vd = vd.replace("\\u003d", "=").replace("\\u0026", "&")
    open("/root/hellapi/.visitor_data", "w").write(vd)
    print("FOUND", vd[:40], "... (len", len(vd), ")")
else:
    print("NOT_FOUND")
    sys.exit(1)
