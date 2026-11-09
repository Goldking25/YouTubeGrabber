FROM python:3.12-slim

# runtime deps: ffmpeg (audio/video processing) + curl (deno installer)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# deno: JS runtime yt-dlp needs for YouTube extraction
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- -y \
    && ffmpeg -version | head -1 && deno --version | head -1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ytdl_api.py ui.html ./

ENV DOWNLOADS_DIR=/downloads
EXPOSE 8099

# jobs.json lives with the app dir; downloads go to the mounted volume
CMD ["python", "ytdl_api.py", "--out", "/downloads", "--port", "8099"]
