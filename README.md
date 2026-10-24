# Save Hack / Mujib Downloader

Mobile-first media downloader with a static HTML/CSS/JS frontend and a FastAPI + yt-dlp + FFmpeg backend.

## Run locally

Requirements: Docker Desktop and Docker Compose.

```bash
docker compose up --build
```

- Website: http://localhost:8080
- API health: http://localhost:8000/health
- API docs: http://localhost:8000/docs

The frontend uses \`http://localhost:8000\` when opened on localhost. The hosted frontend defaults to \`https://save-hack-api.onrender.com\`. If your Render service uses another URL, open the site with \`?api=https%3A%2F%2FYOUR-API-HOST\` or set \`window.SAVE_HACK_API_BASE\` before loading \`app.js\`.

## Architecture

- Static frontend at repository root, deployed with GitHub Pages.
- FastAPI backend in \`backend/\`.
- yt-dlp for supported source extractors.
- FFmpeg for audio extraction and video/audio merging.
- Asynchronous in-process download jobs with progress polling.
- Docker Compose for local development; Render Blueprint at \`render.yaml\`.

## Download flow

1. The user confirms they have the right to save the media.
2. The API analyzes a supported public HTTPS URL.
3. MP4 users can select best available, 480p, 720p, 1080p, 1440p QHD, or 2160p 4K.
4. The API selects a compatible format at or below the requested height when possible. It does not upscale a lower-resolution source.
5. The frontend polls job status and shows extraction progress, then starts a browser download when the file is ready.
6. Temporary output is deleted after download or job expiry.

MP3 uses the best available audio stream and FFmpeg conversion. The available formats and resolutions depend on the source and can change.

## Security and operational limits

- CORS is configured through \`CORS_ORIGINS\`; the Render Blueprint lists the GitHub Pages origin and local development origins.
- URL validation requires HTTPS, rejects credentials, non-standard ports, IP literals, unsupported hosts, and DNS results that are not globally routable.
- Basic process-local rate limits and concurrent-job limits reduce accidental overload.
- Job state is in process memory. A service restart loses in-flight jobs, so this is an MVP, not a durable queue.
- URL validation is only a first layer against SSRF. Redirects, DNS rebinding, and extractor-specific network behavior mean public deployment still needs network-level outbound filtering, request timeouts, monitoring, and a security review.
- Rate limiting is process-local, not distributed. A public production deployment should use a shared limiter, user quotas, and stronger abuse protection.
- Temporary jobs expire after 30 minutes by default. Output files are deleted after the file response is completed.
- The default limit is 300 MB per file and one concurrent download job. Hosting quotas and timeouts are separate constraints.
- Do not use this service to bypass DRM, paywalls, private-account restrictions, or other access controls. Use only media you own, have permission to download, or are legally entitled to save.

## Tests

Install backend dependencies and test tools, then run:

```bash
pip install -r backend/requirements.txt pytest httpx
pytest -q backend/tests
```

GitHub Actions runs the backend tests on pushes and pull requests that touch backend code or its test workflow.

## Hosting notes

Host the static frontend on GitHub Pages or Cloudflare Pages. Run the Python/FFmpeg backend on a container host or VPS that permits media processing. Cloudflare Workers are suitable for lightweight API/orchestration but not long-running yt-dlp/FFmpeg jobs. Free hosting tiers may sleep, impose quotas, or disallow sustained workloads; check current provider terms.

The Render service domain is expected to be \`https://save-hack-api.onrender.com\` by default. Verify that the service exists and is healthy before expecting the public frontend to download media.
