import os, time
from urllib.parse import urlparse
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp

app = FastAPI()

ORIGINS = [o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET"])

ALLOWED_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "facebook.com", "fb.watch", "x.com", "twitter.com",
)

hits = defaultdict(deque)
def rate_limit(ip, limit=10, window=60):
    now = time.time()
    q = hits[ip]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True

def allowed(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and any(
            host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS
        )
    except Exception:
        return False

@app.get("/")
def health():
    return {"status": "ok"}

@app.get("/info")
def info(url: str, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    if not rate_limit(ip):
        raise HTTPException(429, "অনেক বেশি রিকোয়েস্ট, একটু পরে চেষ্টা করুন")
    if not allowed(url):
        raise HTTPException(400, "এই লিংকটি সাপোর্টেড নয়")

    opts = {"quiet": True, "noplaylist": True, "socket_timeout": 20}
    cookies = os.getenv("COOKIES_FILE")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            d = ydl.extract_info(url, download=False)
    except Exception:
        raise HTTPException(400, "ভিডিও পাওয়া যায়নি (প্রাইভেট বা ব্লকড হতে পারে)")

    items = []
    for f in d.get("formats", []):
        if not f.get("url") or f.get("protocol") in ("m3u8", "m3u8_native"):
            continue
        has_v = f.get("vcodec") not in (None, "none")
        has_a = f.get("acodec") not in (None, "none")
        if has_v and has_a:
            kind = "video"
        elif has_a:
            kind = "audio"
        else:
            continue
        items.append({
            "kind": kind,
            "ext": f.get("ext"),
            "quality": f.get("format_note") or f.get("resolution") or "",
            "size": f.get("filesize") or f.get("filesize_approx"),
            "url": f["url"],
        })
    return {"title": d.get("title"), "thumbnail": d.get("thumbnail"), "items": items}
