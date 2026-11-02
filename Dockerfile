FROM python:3.12-slim

LABEL org.opencontainers.image.title="snapchat-export-tools" \
      org.opencontainers.image.description="Turn a Snapchat My Data export into dated, geotagged photos and videos" \
      org.opencontainers.image.authors="Bryan Wodi" \
      org.opencontainers.image.source="https://github.com/talk2bryan/snapchat-export-tools"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY snap_memories.py snap_chat_media.py ./

# docker run -v ~/snap-export:/in:ro -v ~/snap-out:/out IMAGE /out /in [--chat-media]
ENTRYPOINT ["python", "/app/snap_memories.py"]
