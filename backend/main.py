import os, time, glob, shutil, tempfile, threading
from urllib.parse import urlparse, quote
from collections import defaultdict, deque, OrderedDict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import yt_dlp

app = FastAPI()

ORIGINS = [o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGIN", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=ORIGINS, allow_methods=["GET"])

PUBLIC_API = os.getenv("PUBLIC_API", "https://idownloadweb-api.onrender.com").rstrip("/")
MAX_MB = int(os.getenv("MAX_MB", "150"))
MAX_SECONDS = int(os.getenv("MAX_SECONDS", "900"))

# Render Secret File (read-only). Same path as the Telegram bot.
COOKIE_SRC = os.getenv("COOKIES_FILE", "/etc/secrets/cookies.txt")

ALLOWED_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "facebook.com", "fb.watch", "x.com", "twitter.com",
)

# ---------- Rate limit ----------
hits = defaultdict(deque)
hits_lock = threading.Lock()

def rate_limit(key, limit=10, window=60):
    now = time.time()
    with hits_lock:
        q = hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(hits) > 5000:
            for k in [k for k, v in hits.items() if not v or now - v[-1] > window]:
                del hits[k]
        return True

def get_ip(request: Request):
    fallback = request.client.host if request.client else "unknown"
    return request.headers.get("x-forwarded-for", fallback).split(",")[0].strip()

# ---------- Cache (same link, 5 minutes) ----------
CACHE_TTL, CACHE_MAX = 300, 200
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

info_slots = threading.BoundedSemaphore(4)
dl_slots = threading.BoundedSemaphore(2)

def allowed(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme in ("http", "https") and any(
            host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS
        )
    except Exception:
        return False

def base_opts():
    # No cookies here: cookies are only used as a retry (see run_ydl)
    return {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 20, "retries": 2}

def clean_dir(outdir):
    for f in glob.glob(os.path.join(outdir, "*")):
        try:
            os.remove(f)
        except Exception:
            pass

def run_ydl(url, download=False, extra=None, outdir=None):
    """Try WITHOUT cookies first. If it fails and the cookie file exists,
    retry WITH cookies. Full errors go to the Render logs only."""

    def attempt(use_cookies):
        opts = base_opts()
        opts.update(extra or {})
        cpath = None
        if use_cookies:
            # yt-dlp may write to the cookie file, and /etc/secrets is read-only,
            # so use a private temporary copy for every attempt.
            fd, cpath = tempfile.mkstemp(prefix="ck_", suffix=".txt")
            os.close(fd)
            shutil.copy(COOKIE_SRC, cpath)
            opts["cookiefile"] = cpath
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=download)
        finally:
            if cpath:
                try:
                    os.remove(cpath)
                except Exception:
                    pass

    try:
        d = attempt(False)
        print("FETCH OK: without cookies", flush=True)
        return d
    except Exception as e:
        if not os.path.exists(COOKIE_SRC):
            print("ERROR (no cookie file):", repr(e)[:600], flush=True)
            raise
        print("RETRY WITH COOKIES. First error:", repr(e)[:300], flush=True)
        if outdir:
            clean_dir(outdir)
        try:
            d = attempt(True)
            print("FETCH OK: with cookies", flush=True)
            return d
        except Exception as e2:
            print("ERROR:", repr(e2)[:600], flush=True)
            raise

@app.get("/")
def health():
    return {"status": "ok"}

@app.get("/info")
def info(url: str, request: Request):
    if not rate_limit(get_ip(request), 10, 60):
        raise HTTPException(429, "Too many requests. Please try again in a minute.")
    if not allowed(url):
        raise HTTPException(400, "This link is not supported.")

    cached = cache_get(url)
    if cached:
        return cached

    if not info_slots.acquire(blocking=False):
        raise HTTPException(503, "Server is busy. Please try again in a few seconds.")
    try:
        try:
            d = run_ydl(url, download=False)
        except Exception as e:
            print("INFO ERROR:", repr(e)[:600], flush=True)
            raise HTTPException(400, "Video not found. It may be private or blocked.")
    finally:
        info_slots.release()

    dur = d.get("duration")
    if dur and dur > MAX_SECONDS:
        raise HTTPException(400, f"Video is too long (limit {MAX_SECONDS // 60} minutes).")

    fmts = d.get("formats") or []
    has_video = any(f.get("vcodec") not in (None, "none") for f in fmts)
    has_audio = any(f.get("acodec") not in (None, "none") for f in fmts)
    heights = sorted({f["height"] for f in fmts
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    heights = [h for h in heights if h <= 1080][:4] or heights[:1]

    q = quote(url, safe="")
    items = []
    for h in heights:
        items.append({"kind": "video", "ext": "mp4", "quality": f"{h}p", "size": None,
                      "url": f"{PUBLIC_API}/download?url={q}&kind=video&height={h}"})
    if not heights and has_video:
        items.append({"kind": "video", "ext": "mp4", "quality": "Best", "size": None,
                      "url": f"{PUBLIC_API}/download?url={q}&kind=video&height=0"})
    if has_audio:
        items.append({"kind": "audio", "ext": "m4a", "quality": "Best", "size": None,
                      "url": f"{PUBLIC_API}/download?url={q}&kind=audio"})

    result = {"title": d.get("title"), "thumbnail": d.get("thumbnail"), "items": items}
    cache_set(url, result)
    return result

@app.get("/download")
def download(url: str, request: Request, kind: str = "video", height: int = 0):
    if not rate_limit("dl:" + get_ip(request), 5, 60):
        raise HTTPException(429, "Too many downloads. Please try again in a minute.")
    if not allowed(url):
        raise HTTPException(400, "This link is not supported.")
    if kind not in ("video", "audio"):
        raise HTTPException(400, "Invalid request.")
    if not dl_slots.acquire(blocking=False):
        raise HTTPException(503, "Server is busy. Please try again in a few seconds.")

    tmp = tempfile.mkdtemp(prefix="dl_")
    ok = False
    try:
        extra = {
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "max_filesize": MAX_MB * 1024 * 1024,
        }
        if kind == "audio":
            extra["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        else:
            extra["merge_output_format"] = "mp4"
            if height > 0:
                extra["format"] = (f"bv*[height<={height}][vcodec^=avc1]+ba[ext=m4a]/"
                                   f"bv*[height<={height}]+ba/b[height<={height}]/b")
            else:
                extra["format"] = "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*+ba/b"
        try:
            d = run_ydl(url, download=True, extra=extra, outdir=tmp)
        except Exception as e:
            print("DOWNLOAD ERROR:", repr(e)[:600], flush=True)
            raise HTTPException(400, "Download failed. The video may be private or blocked.")

        files = [p for p in glob.glob(os.path.join(tmp, "*"))
                 if not p.endswith((".part", ".ytdl", ".temp"))]
        if not files:
            raise HTTPException(413, f"File is too large (limit {MAX_MB} MB) or unavailable.")
        path = max(files, key=os.path.getsize)
        ok = True
    finally:
        dl_slots.release()
        if not ok:
            shutil.rmtree(tmp, ignore_errors=True)

    title = (d.get("title") or "video")
    name = "".join(c for c in title if c.isalnum() or c in " -_")[:60].strip() or "video"
    ext = os.path.splitext(path)[1] or ".mp4"
    return FileResponse(path, filename=f"{name}{ext}",
                        background=BackgroundTask(shutil.rmtree, tmp, True))
