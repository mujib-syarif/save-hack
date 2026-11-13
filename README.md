# Save Hack: Universal Web Downloader

Mobile-first static frontend (HTML/CSS/JS) plus a modular FastAPI backend that downloads **publicly accessible** media and files from as many sources as technically possible, and clearly reports the ones it cannot handle. Support is never promised per site: a source only counts as supported when an extraction actually succeeds.

## How it works
`POST /api/analyze` → the router picks an engine → the user chooses a format → `POST /api/download` starts a job → the browser polls `GET /api/jobs/{id}` → `GET /api/download/{id}` streams the finished file to disk.

### Extraction engines (`backend/savehack/`)
| Engine | File | Handles |
|---|---|---|
| A. Direct | `engines/direct.py` | Any public file URL (media, images, PDF, archives…): redirects, Content-Type/Content-Disposition naming, HTTP Range + If-Range resume, streaming to disk, size limits |
| B. yt-dlp | `engines/ytdlp_engine.py` | Whatever yt-dlp's own extractor registry claims (no hard-coded site list): format discovery, MP4/MP3, subtitles, playlists (listing), concurrent fragments, retries with backoff |
| C. HTML parser | `engines/html_parser.py` | Open Graph / Twitter Card, JSON-LD, `<video>/<audio>/<source>/<track>/<img>`, download links, embeds, media URLs in scripts. Every candidate is probed; arbitrary HTML links are never downloaded |
| D. HLS / DASH | `engines/streams.py` | Master playlist and MPD parsing: qualities, audio tracks, codecs. Segments are fetched by yt-dlp's *native* downloader and merged with FFmpeg. DRM and live streams are refused |
| E. Site plugins | `sites/` | Pixeldrain, Dropbox, Google Drive (public files), OneDrive (share API), MediaFire (public pages). One small module each; add a site by adding a module and listing it in `sites/__init__.py` |

### Router order (`router.py`)
1. validate + normalise URL (SSRF checks) → 2. manifest / direct file by extension → 3. site plugin → 4. yt-dlp (only when its registry has a dedicated extractor) → 5. generic HTML parsing (+ one level of embedded players) → 6. structured error with per-engine diagnostics.
A URL claimed by a plugin or yt-dlp that then fails is reported with *that* failure and is **not** retried through the HTML parser, so a poster image can never be returned in place of the real media. Identical concurrent analyses share one extraction; results are cached for 5 minutes.

## API
| Endpoint | Purpose |
|---|---|
| `GET /health` | Status, versions, engines, storage, limits |
| `POST /api/analyze` `{url, rights_confirmed}` | → `{job_id, media, formats[], default_format}` |
| `GET /api/formats/{job_id}` | Format list |
| `POST /api/download` `{job_id, format_id}` | Start (or retry) a download → 202 |
| `GET /api/jobs/{job_id}` | Status + progress (`queued/running/done/failed/cancelled`) |
| `GET /api/download/{job_id}` | Stream the finished file |
| `DELETE /api/jobs/{job_id}` | Cancel and delete |
| `POST /analyze`, `POST /download` | Legacy endpoints for older clients |

Clients can only choose a `format_id` the server itself generated; they cannot supply URLs, selectors or paths for the download step. Errors are always `{ok:false, error:{code,message,retryable}, detail}` with the codes `INVALID_URL, UNSUPPORTED_SOURCE, MEDIA_NOT_FOUND, FORMAT_UNAVAILABLE, SOURCE_BLOCKED, AUTHENTICATION_REQUIRED, RATE_LIMITED, NETWORK_ERROR, EXTRACTION_TIMEOUT, DOWNLOAD_FAILED, CONVERSION_FAILED, STORAGE_LIMIT, SERVER_ERROR`. Technical detail is logged server-side only.

## Configuration (environment variables)
`DOWNLOAD_DIR` (/tmp/downloads) · `ALLOWED_ORIGINS` (comma-separated; **defaults to `*`, set it to the frontend origin in production**) · `MAX_DOWNLOAD_MB` (300) · `MAX_STORAGE_MB` (1024) · `MAX_CONCURRENT_JOBS` (2) · `MAX_QUEUED_JOBS` (6) · `MAX_ACTIVE_JOBS_PER_IP` (2) · `ANALYZE_TIMEOUT` (45s) · `HTTP_TIMEOUT` (15s) · `JOB_TIMEOUT` (900s) · `MAX_RETRIES` (3) · `FRAGMENT_CONCURRENCY` (4) · `JOB_TTL_SECONDS` (900) · `ANALYSIS_CACHE_SECONDS` (300) · `RATE_ANALYZE_PER_MIN` (20) · `RATE_DOWNLOAD_PER_MIN` (8) · `TRUSTED_PROXY_HOPS` (1 on Railway, 0 when not behind a proxy) · `ENABLE_DOCS` (off; set `1` to expose `/docs`) · `LEGACY_DOWNLOAD_WAITERS` (4) · `LOG_LEVEL`.
Concurrent fragments do not automatically make downloads faster; speed is bounded by the source server and the network.

## Security model
- Only `http`/`https` on ports 80/443, no credentials in URLs, blocked internal host names, URL length/control-character checks.
- **SSRF:** every address a host resolves to must be globally routable (loopback, private, link-local, CGNAT, multicast, cloud metadata, IPv4-mapped/NAT64/6to4 tricks are rejected). Redirects are followed manually and re-validated at every hop. While a download or extraction runs, name resolution is guarded so DNS rebinding cannot reach non-public addresses. yt-dlp is forced to its native downloaders (no external ffmpeg/aria2 fetching, which would bypass the guard). Environment proxies are ignored.
- Per-IP rate limits, global and per-client job limits, per-file and total-storage limits, request and job timeouts, no shell invocation, safe file names, downloaded files served as opaque attachments, DTD/entity-free manifest parsing.
- No cookies, passwords or sessions are stored or accepted. DRM, paywalls, logins, private accounts, CAPTCHA and anti-bot checks are **not** bypassed; they are reported as `SOURCE_BLOCKED` / `AUTHENTICATION_REQUIRED`.
- Not covered by the in-process guard: clients that bypass Python's `socket`. Keep curl-impersonation backends uninstalled and add network-level egress rules for a public deployment.

## Deployment (Railway)
Root directory `backend/`; `Dockerfile` + `railway.toml` (health check `/health`). Python 3.12, FFmpeg installed in the image, yt-dlp installed at build time (rebuild to pick up extractor fixes). Run **one** process: job state and rate limits are in memory. Storage is ephemeral: job directories are swept after `JOB_TTL_SECONDS`, purged at startup/shutdown, and capped by `MAX_STORAGE_MB`.

## Local development
```bash
docker compose up --build        # site http://localhost:8080, API http://localhost:8000/docs
cd backend && pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
```
Tests use a local HTTP server and generated fixtures (no external sites). API tests need `fastapi`+`httpx`; the yt-dlp integration tests need `yt-dlp`, `ffmpeg` and `ffprobe` and are skipped otherwise.

## Known limitations
- Platforms such as YouTube may block datacenter IPs or demand verification; this is reported, not bypassed.
- Playlists are listed, not bulk-downloaded. Live streams and DRM content are unsupported. Google Drive large-file confirmation pages and MediaFire page layout changes can break those plugins.
- Analysis timeouts stop waiting for a slow extractor but cannot kill its worker thread; network timeouts bound it.
- Use only media you own or are entitled to save. Review each platform's terms and applicable law before enabling it publicly.
