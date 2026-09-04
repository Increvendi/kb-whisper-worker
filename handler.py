"""
RunPod serverless-worker: KB-Whisper large (faster-whisper, fp16) -> Supabase.

Två lägen, valt per jobb via input.mode (fallback env MODE, default "pi"):
  pi  : Prorespektera Intelligence. recording_id = recordings.cm_recording_id (bigint).
        Skriver transcripts (upsert på cm_recording_id) och recordings.transcribe_status.
  crm : Prorespektera CRM (möten). recording_id = crm_meeting_recordings.id (uuid).
        Skriver crm_transcript_segments, textfil i bucket meeting-transcripts, status på raden.

Input : {"mode": "pi"|"crm", "recording_id": ..., "audio_url": "<signerad url>",
         "language": "sv", "words": false}
Env   : SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (sätts på endpointen), MODE (valfri),
        GPU_USD_PER_HOUR (valfri, för cost_usd; default 0.80), MODEL_ID (valfri)
"""
import os, time, tempfile, subprocess, traceback
import requests
import runpod
from faster_whisper import WhisperModel

MODEL_ID   = os.environ.get("MODEL_ID", "KBLab/kb-whisper-large")
MODEL_DIR  = "/models"                      # förladdad i Dockerfile
DEFAULT_MODE = os.environ.get("MODE", "pi")
GPU_USD_H  = float(os.environ.get("GPU_USD_PER_HOUR", "0.80"))
SB_URL     = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY     = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HDR        = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}
JSON_HDR   = {**HDR, "Content-Type": "application/json", "Prefer": "return=minimal"}

# Laddas en gång per container (varm mellan jobb)
model = WhisperModel(MODEL_ID, device="cuda", compute_type="float16", download_root=MODEL_DIR)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def to_wav16k(src: str) -> str:
    dst = src + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000", dst],
        check=True,
    )
    return dst


def transcribe(path: str, language: str, want_words: bool):
    segments, info = model.transcribe(
        path,
        language=language,
        beam_size=5,
        vad_filter=True,                       # klipper tystnad -> färre hallucinationer
        word_timestamps=want_words,
        condition_on_previous_text=False,      # KBLab:s rekommendation
    )
    out = []
    for i, s in enumerate(segments):
        seg = {
            "seq": i,
            "start_ms": int(s.start * 1000),
            "end_ms": int(s.end * 1000),
            "text": s.text.strip(),
        }
        if want_words:
            seg["words"] = [{"w": w.word.strip(), "s": int(w.start * 1000), "e": int(w.end * 1000)}
                            for w in (s.words or [])]
        out.append(seg)
    return out, int(info.duration * 1000)


def download(url: str, dst: str):
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dst, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


# ---------- PI ----------
def pi_patch(rid, body: dict):
    r = requests.patch(f"{SB_URL}/rest/v1/recordings?cm_recording_id=eq.{rid}",
                       headers=JSON_HDR, json=body, timeout=30)
    r.raise_for_status()


def pi_write(rid, segs, duration_ms, gpu_s, lang):
    row = {
        "cm_recording_id": rid,
        "language": lang,
        "model": MODEL_ID,
        "full_text": "\n".join(s["text"] for s in segs),
        "segments": segs,
        "duration_minutes": round(duration_ms / 60000, 2),
        "cost_usd": round(gpu_s / 3600 * GPU_USD_H, 5),
    }
    r = requests.post(
        f"{SB_URL}/rest/v1/transcripts?on_conflict=cm_recording_id",
        headers={**JSON_HDR, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=row, timeout=60,
    )
    r.raise_for_status()
    pi_patch(rid, {
        "transcribe_status": "klar",
        "transcribe_finished_at": now_iso(),
        "transcribe_gpu_seconds": round(gpu_s, 1),
        "transcribe_error": None,
    })


# ---------- CRM ----------
def crm_patch(rid, body: dict):
    r = requests.patch(f"{SB_URL}/rest/v1/crm_meeting_recordings?id=eq.{rid}",
                       headers=JSON_HDR, json=body, timeout=30)
    r.raise_for_status()


def crm_write(rid, segs, duration_ms, lang):
    text = "\n".join(s["text"] for s in segs)
    path = f"{rid}/transcript.txt"
    r = requests.post(
        f"{SB_URL}/storage/v1/object/meeting-transcripts/{path}",
        headers={**HDR, "Content-Type": "text/plain; charset=utf-8", "x-upsert": "true"},
        data=text.encode("utf-8"), timeout=60,
    )
    r.raise_for_status()
    requests.delete(f"{SB_URL}/rest/v1/crm_transcript_segments?recording_id=eq.{rid}",
                    headers=HDR, timeout=30).raise_for_status()
    rows = [{"recording_id": rid, **s} for s in segs]
    for i in range(0, len(rows), 500):
        requests.post(f"{SB_URL}/rest/v1/crm_transcript_segments",
                      headers=JSON_HDR, json=rows[i:i + 500], timeout=60).raise_for_status()
    crm_patch(rid, {
        "status": "klar",
        "transcript_path": path,
        "duration_ms": duration_ms,
        "model_id": MODEL_ID,
        "transcribe_finished_at": now_iso(),
        "transcribe_error": None,
    })


def handler(job):
    inp = job["input"]
    mode = inp.get("mode", DEFAULT_MODE)
    rid, url, lang = inp["recording_id"], inp["audio_url"], inp.get("language", "sv")
    want_words = bool(inp.get("words", mode == "crm"))   # ordnivå default bara för möten
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "audio.bin")
            download(url, src)
            wav = to_wav16k(src)
            segs, duration_ms = transcribe(wav, lang, want_words)
        gpu_s = time.time() - t0

        if mode == "pi":
            pi_write(rid, segs, duration_ms, gpu_s, lang)
        elif mode == "crm":
            crm_write(rid, segs, duration_ms, lang)
        else:
            raise ValueError(f"okänt mode: {mode}")

        return {"mode": mode, "recording_id": rid, "segments": len(segs),
                "audio_s": duration_ms // 1000, "wall_s": round(time.time() - t0, 1)}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        try:
            if mode == "pi":
                pi_patch(rid, {"transcribe_status": "fel", "transcribe_error": err[:1000]})
            else:
                crm_patch(rid, {"status": "fel", "transcribe_error": err[:1000]})
        except Exception:
            pass
        return {"error": err, "trace": traceback.format_exc()[-2000:]}


runpod.serverless.start({"handler": handler})
