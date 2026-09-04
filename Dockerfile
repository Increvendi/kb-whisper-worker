# Bygg: docker build -t <ditt-repo>/kb-whisper-worker:1 .  && docker push ...
FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir runpod==1.7.* faster-whisper==1.1.* requests

# Förladda modellen i imagen så kaltstarten inte hämtar 3 GB varje gång
RUN python3 -c "from faster_whisper import WhisperModel; \
    WhisperModel('KBLab/kb-whisper-large', device='cpu', compute_type='int8', download_root='/models')"

WORKDIR /app
COPY handler.py .
CMD ["python3", "-u", "handler.py"]
