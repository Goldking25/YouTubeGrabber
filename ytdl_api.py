"""
YouTubeGrabber - friendly YouTube downloader (audio + video) with a local web UI.

Features:
  - Video (MP4 best quality) or Audio (MP3) downloads
  - Per-download output folder selection
  - Pause / Resume downloads (yt-dlp continues .part files)
  - Crash recovery: job state persisted to jobs.json; interrupted jobs can be
    resumed after unexpected termination
  - Live progress (percent, speed, ETA)

Usage:
    py ytdl_api.py --out "C:\\Users\\manig\\Downloads\\ytgrab" [--port 8099]

Only use for content you have the right to download. Not for piracy.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI(title="YouTubeGrabber")

STATE_FILE = Path(os.environ.get("YTDL_STATE", str(Path(__file__).with_name("jobs.json"))))
DEFAULT_OUT = Path.home() / "Downloads" / "ytgrab"
OUTPUT_DIR = DEFAULT_OUT

MAX_CONCURRENT = 1  # how many downloads run at once; the rest wait in queue

JOBS: dict[str, dict] = {}
PROCS: dict[str, subprocess.Popen] = {}
LOCK = threading.Lock()

# Windows-only flag to hide child console windows; None elsewhere (Linux/macOS)
PROC_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

PCT_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def save_jobs():
    with LOCK:
        STATE_FILE.write_text(json.dumps(JOBS, indent=1), encoding="utf-8")


def load_jobs():
    if not STATE_FILE.exists():
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for jid, j in data.items():
        if j.get("status") in ("downloading", "merging"):
            j["status"] = "interrupted"
        j["percent"] = j.get("percent", 0)
        JOBS[jid] = j


def build_cmd(url: str, mode: str, outdir: Path, limit: int = 0) -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--newline", "--progress",
           "-o", str(outdir / "%(title)s.%(ext)s"), "--windows-filenames",
           "--trim-filenames", "180", "--no-restrict-filenames"]
    if mode == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "192"]
    else:
        cmd += ["-f", "bestvideo*+bestaudio/best", "--merge-output-format", "mp4"]
    if limit and limit > 0:
        cmd += ["--playlist-items", f"1:{int(limit)}"]
    cmd += [url]
    return cmd


def monitor(job_id: str, proc: subprocess.Popen, outdir: Path):
    j = JOBS[job_id]
    merging = False
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").strip()
        m = PCT_RE.search(line)
        if m and ("download" in line.lower() or "%" in line):
            with LOCK:
                j["percent"] = float(m.group(1))
                sp = re.search(r"at\s+([\d.]+[KMG]?i?B/s)", line)
                if sp:
                    j["speed"] = sp.group(1)
                et = re.search(r"ETA\s+([\d:]+)", line)
                if et:
                    j["eta"] = et.group(1)
                # after a merger line, new progress = next playlist item started
                if merging and j["status"] == "merging":
                    j["status"] = "downloading"
                    merging = False
        it = re.search(r"Downloading item (\d+) of (\d+)", line)
        if it:
            with LOCK:
                j["item"] = f"{int(it.group(1))}/{int(it.group(2))}"
        if line.startswith("[download] Destination:") or line.startswith("[ExtractAudio] Destination:"):
            with LOCK:
                t = Path(line.split("Destination:", 1)[1].strip().strip('"')).stem
                j["title"] = re.sub(r"\.f\d+$", "", t)
        if line.startswith("[Merger]") or line.startswith("[ExtractAudio]"):
            merging = True
            with LOCK:
                if j["status"] == "downloading":
                    j["status"] = "merging"
    proc.wait()
    with LOCK:
        if j["status"] == "cancelled":
            pass  # stop() owns the status now
        elif j["status"] == "stopping":
            j["status"] = "paused"
        elif proc.returncode == 0:
            j["status"] = "done"
            j["percent"] = 100
        else:
            j["status"] = "error"
            j["error"] = f"yt-dlp exited with code {proc.returncode}"
    PROCS.pop(job_id, None)
    save_jobs()
    dispatch()  # start the next queued job, if any


def running_count() -> int:
    return sum(1 for p in PROCS.values() if p.poll() is None)


def dispatch():
    """Move queued jobs into 'downloading' while slots are free (FIFO)."""
    while True:
        with LOCK:
            if running_count() >= MAX_CONCURRENT:
                return
            nxt = None
            for j in sorted(JOBS.values(), key=lambda x: x.get("started", "")):
                if j["status"] == "queued":
                    nxt = j
                    break
            if not nxt:
                return
            # launch INSIDE the lock to guarantee the 1-at-a-time limit
            _start_proc(nxt)


def _start_proc(j: dict):
    """Register and spawn the yt-dlp process for job j. Caller must hold LOCK."""
    job_id = j["id"]
    outdir = Path(j["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        build_cmd(j["url"], j["mode"], outdir, int(j.get("limit") or 0)),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=PROC_FLAGS,
    )
    PROCS[job_id] = proc
    j.update(status="downloading", error=None)
    threading.Thread(target=monitor, args=(job_id, proc, outdir), daemon=True).start()


def launch(job_id: str):
    with LOCK:
        j = JOBS[job_id]
        _start_proc(j)
    save_jobs()


def safe_outdir(raw: str | None) -> Path:
    if raw and raw.strip():
        p = Path(raw.strip()).expanduser().resolve()
        if not p.is_absolute():
            raise HTTPException(400, f"folder must be an absolute path, got: {p}")
        if not p.is_dir():
            raise HTTPException(400, f"folder does not exist: {p}")
        return p
    return OUTPUT_DIR


def job_key(url: str, mode: str) -> str:
    """Normalizes a YouTube URL so re-submits of the same video/playlist match.
    Key = mode | videoId | playlistId"""
    video = re.search(r"[?&]v=([\w-]{6,})", url) or re.search(r"youtu\.be/([\w-]{6,})", url)
    playlist = re.search(r"[?&]list=([\w-]+)", url)
    return f"{mode}|{video.group(1) if video else url.strip().rstrip('/')}|{playlist.group(1) if playlist else ''}"


def find_existing(key: str):
    """Returns (job, kind): same key already active or already downloaded."""
    for j in JOBS.values():
        if j.get("key") != key:
            continue
        if j["status"] in ("downloading", "merging", "stopping", "queued"):
            return j, "active"
        if j["status"] == "done":
            return j, "done"
        # paused / interrupted / error -> safe to start again
    return None, None


# ---------------- API ----------------

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(Path(__file__).with_name("ui.html").read_text(encoding="utf-8"))


@app.post("/api/start")
def start(url: str, mode: str = "video", out_dir: str = "", limit: int = 0):
    if mode not in ("audio", "video"):
        raise HTTPException(400, "mode must be audio or video")
    if not url.strip():
        raise HTTPException(400, "url required")
    outdir = safe_outdir(out_dir)
    key = job_key(url, mode)
    existing, kind = find_existing(key)
    if kind == "active":
        raise HTTPException(409, f"This URL is already downloading right now (job {existing['id']}). Resume it or wait.")
    job_id = uuid.uuid4().hex[:8]
    with LOCK:
        JOBS[job_id] = {
            "id": job_id, "url": url.strip(), "mode": mode, "key": key, "outdir": str(outdir),
            "limit": max(0, int(limit or 0)),
            "status": "queued", "percent": 0, "speed": "?", "eta": "?", "error": None,
            "title": url[:60],
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    dispatch()
    save_jobs()
    return {"id": job_id}


@app.post("/api/resume/{job_id}")
def resume(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if PROCS.get(job_id):
        raise HTTPException(400, "job already running")
    with LOCK:
        if running_count() >= MAX_CONCURRENT:
            j["status"] = "queued"
            save_jobs()
            return {"ok": True, "queued": True}
        _start_proc(j)
    save_jobs()
    return {"ok": True}


@app.post("/api/pause/{job_id}")
def pause(job_id: str):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    proc = PROCS.get(job_id)
    if not proc:
        raise HTTPException(400, "job not running")
    with LOCK:
        j["status"] = "stopping"
    proc.terminate()  # .part file remains -> resume continues from it
    return {"ok": True}


@app.post("/api/stop/{job_id}")
def stop(job_id: str, delete: bool = True):
    """Cancel the download. Optionally delete partial files (.part/.ytdl)."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    outdir = Path(j["outdir"])
    proc = PROCS.get(job_id)
    with LOCK:
        # set BEFORE kill so the monitor thread won't overwrite it with "paused"
        j["status"] = "cancelled"
    if proc:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
    deleted = 0
    if delete and outdir.is_dir():
        for p in list(outdir.glob("*.part")) + list(outdir.glob("*.ytdl")) + list(outdir.glob("*.tmp")):
            try:
                p.unlink()
                deleted += 1
            except OSError:
                pass
    save_jobs()
    dispatch()
    return {"ok": True, "deleted_partials": deleted}


@app.post("/api/remove/{job_id}")
def remove(job_id: str):
    """Remove a job from the list entirely (only if not running)."""
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if PROCS.get(job_id) and PROCS[job_id].poll() is None:
        raise HTTPException(400, "job is running — stop it first, then remove")
    with LOCK:
        JOBS.pop(job_id, None)
        PROCS.pop(job_id, None)
    save_jobs()
    return {"ok": True}


@app.get("/api/status")
def status():
    with LOCK:
        out = {}
        for jid, j in sorted(JOBS.items(), key=lambda kv: kv[1].get("started", ""), reverse=True):
            out[jid] = {k: j.get(k) for k in
                        ("status", "title", "url", "mode", "outdir", "percent", "speed", "eta", "error", "started", "item")}
        return out


def main():
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="YouTubeGrabber web UI")
    parser.add_argument("--out", default=str(OUTPUT_DIR), help="default download folder")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    OUTPUT_DIR = Path(args.out).expanduser().resolve()
    load_jobs()
    dispatch()  # resume any queued jobs from a previous session
    print(f"\n  YouTubeGrabber  ->  default folder: {OUTPUT_DIR}")
    print(f"  Open http://localhost:{args.port} in your browser\n")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
