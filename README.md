# Save Hack / Mujib Downloader

Mobile-first media downloader MVP with a static HTML/CSS/JS frontend and a separate FastAPI + yt-dlp + FFmpeg backend.

## Run locally
Requirements: Docker Desktop and Docker Compose.

```bash
docker compose up --build
```

- Website: http://localhost:8080
- API health: http://localhost:8000/health
- API docs: http://localhost:8000/docs

## Architecture
- Static frontend at repository root.
- FastAPI backend in `backend/`.
- yt-dlp for supported source extractors.
- FFmpeg for audio extraction/conversion and media processing.
- Docker Compose for local development.

`API_BASE` in `app.js` is empty by default, so set it to the API origin (for local Docker Compose: `http://localhost:8000`). Set `ALLOWED_ORIGINS` (comma-separated) on the API to the frontend's exact origin; without it the API allows any origin (`*`). Docker Compose binds the API to `127.0.0.1` only.

## Scope and limitations
- MP4/MP3 options depend on what the source provides; not every URL or resolution will work.
- Platform extractor support can change. YouTube, TikTok, Instagram and other sources are not guaranteed to work for every URL.
- Use only media you own, have permission to download, or are legally entitled to save.
- Do not use this service to bypass DRM, paywalls, private-account restrictions, or other access controls.
- This MVP has no persistent queue, authentication, distributed rate limiter, or hardened public abuse protection. Do not expose it to the public before those are implemented and tested.
- Initial app limit: 300 MB per file and one concurrent job. These are application settings, not hosting-provider quotas.
- SSRF defense has two layers: URL validation, plus a connection-level guard that makes name resolution during yt-dlp (including redirects and DNS rebinding) return public IPs only. It does not cover clients that bypass Python's `socket` (for example curl-impersonation backends), so public deployment still requires network-level egress controls and security review.

## Low-cost hosting plan
Host the static frontend on Cloudflare Pages or GitHub Pages. Run the Python/FFmpeg backend on a container host or VPS that permits media processing. Cloudflare Workers are suitable for lightweight API/orchestration but are not the runtime for a long-running yt-dlp/FFmpeg process. Free backend tiers may sleep, impose quotas, or disallow sustained workloads; check current provider terms.

## Before public launch
1. Set `ALLOWED_ORIGINS` to the exact frontend origin (the default is wildcard).
2. Add a persistent queue, per-IP/user rate limits, quotas, job timeout and concurrency limits.
3. Add network-level SSRF protection and restrict outbound destinations.
4. Add temporary-storage lifecycle and verify files are deleted after completed downloads.
5. Add abuse reporting, privacy notice, terms of use, copyright/takedown handling and monitoring.
6. Review current platform terms and applicable law before enabling each source publicly.
