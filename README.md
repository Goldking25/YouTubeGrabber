# YouTubeGrabber

Friendly local web app to download YouTube videos & playlists as **MP4 (best quality)** or **MP3**, built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) + FastAPI.

> ⚠️ For personal offline use with content you have the right to download (own uploads, Creative Commons, etc.). Not for piracy or DRM-protected platforms (Netflix etc. are not and cannot be supported).

## Features

- 🎬 Video (MP4) / 🎵 Audio (MP3) modes
- 📁 Per-download output folder (absolute path) or `--out` default
- 🕰 Download queue (1 concurrent, FIFO auto-advance, "Start now" to jump the line)
- ⏸/▶ Pause & Resume (continues `.part` files)
- 🛑 Stop & delete partial files
- 🗑 Remove finished/stopped jobs from the list
- 🚫 Duplicate detection (URL normalized by video/playlist ID)
- 💥 Crash recovery — job state persisted to `jobs.json`, interrupted jobs resumable
- 📊 Live progress: percent, speed, ETA, `[song 12/46]` playlist counter
- 🚿 Playlist limit (e.g. 20) to tame 400+-song radio mixes
- 📑 Tabs: All / Downloading / Queued / Completed with live counts
- 🐳 Dockerfile + compose for containerized runs

## Quick start (bare Python 3.12+)

```powershell
py -m pip install -r requirements.txt   # plus ffmpeg + deno on PATH
py ytdl_api.py --out "C:\some\folder" --port 8099
```

Open **http://localhost:8099**

Daemon requirements: **ffmpeg** (merging/MP3), **deno** (JS runtime yt-dlp needs for YouTube).

## Quick start (Docker)

```powershell
docker compose up -d --build
```

Files land in the host folder mounted at `/downloads` (see `docker-compose.yml`).

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/start?url=&mode=video|audio&out_dir=&limit=` | start (409 if duplicate active) |
| `POST /api/pause/{id}` / `POST /api/resume/{id}` | pause / resume |
| `POST /api/stop/{id}?delete=true` | cancel + delete partials |
| `POST /api/remove/{id}` | remove non-running job from list |
| `GET /api/status` | all jobs |

## Project layout

```
ytdl_api.py        FastAPI server + queue + subprocess manager
ui.html            single-page UI (dark theme)
Dockerfile         python:3.12-slim + ffmpeg + deno
docker-compose.yml mount + restart policy
jobs.json          (runtime) persisted job state
```
