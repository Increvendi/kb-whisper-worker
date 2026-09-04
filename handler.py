"""
RunPod serverless-worker: KB-Whisper large (faster-whisper, fp16) -> Supabase.

Input  : {"recording_id": "<uuid>", "audio_url": "<signerad url>", "language": "sv"}
Effekt : rader i crm_transcript_segments, textfil i bucket meeting-transcripts,
         status 'klar' (eller 'fel') på crm_meeting_recordings.
Env    : SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   (sätts på endpointen i RunPod)
"""
import os, json, time, tempfile, subprocess, traceback
import requests
import runpod
from faster_whisper import WhisperModel

MODEL_ID   = os.environ.get("MODEL_ID", "KBLab/kb-whisper-large")
MODEL_DIR  = "/models"                      # förladdad i Dockerfile
SB_URL     = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY     = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HDR        = {"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}

# Laddas en gång per container (varm mellan jobb)
model = WhisperModel(MODEL_ID, device="cuda", compute_type="float16", download_root=MODEL_DIR)


def to_wav16k(src: str) -> str:
    dst = src + ".wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "16000", dst],
        check=True,
    )
    return dst


def transcribe(path: str, language: str):
    segments, info = model.transcribe(
        path,
        language=language,
        beam_size=5,
        vad_filter=True,                       # klipper tystnad -> färre hallucinationer
        word_timestamps=True,
        condition_on_previous_text=False,      # KBLab:s rekommendation
    )
    out = []
    for i, s in enumerate(segments):
        out.append({
            "seq": i,
            "start_ms": int(s.start * 1000),
            "end_ms": int(s.end * 1000),
            "text": s.text.strip(),
            "words": [{"w": w.word.strip(), "s": int(w.start * 1000), "e": int(w.end * 1000)}
                      for w in (s.words or [])],
        })
    return out, int(info.duration * 1000)


def sb_patch(recording_id: str, body: dict):
    r = requests.patch(
        f"{SB_URL}/rest/v1/crm_meeting_recordings?id=eq.{recording_id}",
        headers={**HDR, "Content-Type": "application/json", "Prefer": "return=minimal"},
        json=body, timeout=30,
    )
    r.raise_for_status()


def sb_write_segments(recording_id: str, segs: list):
    # Idempotent: rensa ev. tidigare körning
    requests.delete(
        f"{SB_URL}/rest/v1/crm_transcript_segments?recording_id=eq.{recording_id}",
        headers=HDR, timeout=30,
    ).raise_for_status()
    rows = [{"recording_id": recording_id, **s} for s in segs]
    for i in range(0, len(rows), 500):            # batcha stora möten
        r = requests.post(
            f"{SB_URL}/rest/v1/crm_transcript_segments",
            headers={**HDR, "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=rows[i:i + 500], timeout=60,
        )
        r.raise_for_status()


def sb_upload_text(recording_id: str, text: str) -> str:
    path = f"{recording_id}/transcript.txt"
    r = requests.post(
        f"{SB_URL}/storage/v1/object/meeting-transcripts/{path}",
        headers={**HDR, "Content-Type": "text/plain; charset=utf-8", "x-upsert": "true"},
        data=text.encode("utf-8"), timeout=60,
    )
    r.raise_for_status()
    return path


def handler(job):
    inp = job["input"]
    rid, url, lang = inp["recording_id"], inp["audio_url"], inp.get("language", "sv")
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "audio.bin")
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(src, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            wav = to_wav16k(src)
            segs, duration_ms = transcribe(wav, lang)

        text = "\n".join(s["text"] for s in segs)
        path = sb_upload_text(rid, text)
        sb_write_segments(rid, segs)
        sb_patch(rid, {
            "status": "klar",
            "transcript_path": path,
            "duration_ms": duration_ms,
            "model_id": MODEL_ID,
            "transcribe_finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "transcribe_error": None,
        })
        return {"recording_id": rid, "segments": len(segs),
                "audio_s": duration_ms // 1000, "wall_s": round(time.time() - t0, 1)}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        try:
            sb_patch(rid, {"status": "fel", "transcribe_error": err[:1000]})
        except Exception:
            pass
        return {"error": err, "trace": traceback.format_exc()[-2000:]}


runpod.serverless.start({"handler": handler})
