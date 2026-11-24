import os, time, threading
from urllib.parse import urlparse
from collections import defaultdict, deque, OrderedDict

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

# ---------- Rate limit (per IP) ----------
hits = defaultdict(deque)
hits_lock = threading.Lock()

def rate_limit(ip, limit=10, window=60):
    now = time.time()
    with hits_lock:
        q = hits[ip]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(hits) > 5000:
            stale = [k for k, v in hits.items() if not v or now - v[-1] > window]
            for k in stale:
                del hits[k]
        return True

# ---------- Cache (same link within 5 minutes) ----------
CACHE_TTL = 300
CACHE_MAX = 200
cache = OrderedDict()
cache_lock = threading.Lock()

def cache_get(url):
    with cache_lock:
        item = cache.get(url)
        if item and time.time() - item[0] < CACHE_TTL:
            cache.move_to_end(url)
            return item[1]
        if item:
            del cache[url]
    return None

def cache_set(url, data):
    with cache_lock:
        cache[url] = (time.time(), data)
        cache.move_to_end(url)
        while len(cache) > CACHE_MAX:
            cache.popitem(last=False)

# ---------- Max simultaneous yt-dlp jobs ----------
slots = threading.BoundedSemaphore(4)

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
    fallback_ip = request.client.host if request.client else "unknown"
    ip = request.headers.get("x-forwarded-for", fallback_ip).split(",")[0].strip()

    if not rate_limit(ip):
        raise HTTPException(429, "Too many requests. Please try again in a minute.")
    if not allowed(url):
        raise HTTPException(400, "This link is not supported.")

    cached = cache_get(url)
    if cached:
        return cached

    if not slots.acquire(blocking=False):
        raise HTTPException(503, "Server is busy. Please try again in a few seconds.")

    try:
        opts = {"quiet": True, "noplaylist": True, "socket_timeout": 20}
        cookies = os.getenv("COOKIES_FILE")
        if cookies and os.path.exists(cookies):
            opts["cookiefile"] = cookies

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                d = ydl.extract_info(url, download=False)
        except Exception:
            raise HTTPException(400, "Video not found. It may be private or blocked.")

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

        result = {"title": d.get("title"), "thumbnail": d.get("thumbnail"), "items": items}
    finally:
        slots.release()

    cache_set(url, result)
    return result
