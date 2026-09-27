"""Direct Innertube API test — Chrome visitor_data ke saath (yt-dlp bypass)."""
import json
import urllib.request

VD = open("/root/hellapi/.visitor_data").read().strip()

body = {
    "context": {
        "client": {
            "clientName": "MWEB",
            "clientVersion": "2.20260915.01.00",
            "hl": "en",
            "gl": "US",
            "visitorData": VD,
            "userAgent": "Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        },
        "request": {"internalExperimentFlags": [], "consistencyTokenJars": []},
    },
    "videoId": "aqz-KE-bpKQ",
    "contentCheckOk": True,
    "racyCheckOk": True,
}

req = urllib.request.Request(
    "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
    data=json.dumps(body).encode(),
    headers={
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        "X-Goog-Visitor-Id": VD,
        "X-Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
        "Cookie": "VISITOR_INFO1_LIVE=" in open("/root/hellapi/.visitor_data").read()
                  and "" or "",
    },
)
try:
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    status = data.get("playabilityStatus", {})
    print("HTTP:", resp.status)
    print("playabilityStatus:", status.get("status"))
    print("reason:", (status.get("reason") or "")[:120])
    pd = data.get("streamingData", {})
    fmts = pd.get("adaptiveFormats") or pd.get("formats") or []
    print("formats:", len(fmts), "| hls:", bool(pd.get("hlsManifestUrl")), "| dash:", bool(pd.get("dashManifestUrl")))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read()[:200])
