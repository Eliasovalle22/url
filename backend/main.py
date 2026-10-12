"""Backend Descargalo — FastAPI + yt-dlp.

Endpoints:
  GET  /api/health            -> estado del servicio
  POST /api/info               -> metadata + calidades disponibles
  POST /api/jobs               -> crea un trabajo de descarga (con progreso)
  GET  /api/jobs/{id}          -> estado/progreso del trabajo
  GET  /api/jobs/{id}/file     -> archivo listo para descargar
  DELETE /api/jobs/{id}        -> borra trabajo y archivo temporal
  GET  /api/download           -> descarga directa legacy (sin progreso)
"""

import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    import yt_dlp
except ImportError:  # pragma: no cover
    yt_dlp = None  # type: ignore


app = FastAPI(title="Descargalo API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
TMPDIR = Path(tempfile.gettempdir()) / "descargalo"
TMPDIR.mkdir(parents=True, exist_ok=True)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

# --- Límites configurables por entorno (Render: Environment -> Add) ---
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))  # cola: solo N descargas a la vez
MAX_JOBS = int(os.getenv("MAX_JOBS", "50"))  # tope de trabajos en memoria
JOB_TTL_SEC = int(os.getenv("JOB_TTL_SEC", "1800"))  # 30 min: luego se borra trabajo + archivo
INFO_CACHE_TTL = int(os.getenv("INFO_CACHE_TTL", "600"))  # 10 min de caché para /api/info
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "500"))  # archivos más grandes se rechazan
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

_job_slots = threading.Semaphore(MAX_CONCURRENT_JOBS if MAX_CONCURRENT_JOBS > 0 else 1)

# Caché simple de metadata: url -> (timestamp, data cruda de yt-dlp)
_info_cache: dict[str, tuple[float, dict]] = {}
_info_cache_lock = threading.Lock()


class InfoRequest(BaseModel):
    url: str


class JobRequest(BaseModel):
    url: str
    kind: str = "video"  # video | audio
    quality: str = "best"


_BLOCKED_HOSTS = (
    "localhost", "metadata.google.internal", "metadata.google",
    "169.254.169.254",  # metadata AWS/GCP/Azure
)


def _is_blocked_host(host: str) -> bool:
    h = (host or "").lower().strip().rstrip(".")
    if not h:
        return True
    if h in _BLOCKED_HOSTS or h.endswith(".local") or h.endswith(".internal"):
        return True
    if h.startswith("127.") or h == "::1":
        return True
    if h.startswith("10.") or h.startswith("192.168."):
        return True
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


def _check_url(url: str) -> str:
    url = (url or "").strip()
    if not url or not URL_RE.match(url):
        raise HTTPException(status_code=400, detail="Pega un enlace válido que empiece con http:// o https://")
    if len(url) > 2000:
        raise HTTPException(status_code=400, detail="Ese enlace es demasiado largo.")
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        raise HTTPException(status_code=400, detail="Pega un enlace válido que empiece con http:// o https://")
    if _is_blocked_host(host):
        raise HTTPException(status_code=400, detail="Ese enlace no está permitido.")
    if yt_dlp is None:
        raise HTTPException(status_code=500, detail="Falta instalar dependencias: pip install -r requirements.txt")
    return url


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 2,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }


def _format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _human_size(n) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _friendly_extract_error(url: str, e: Exception) -> str:
    lower_url = (url or "").lower()
    msg = str(e).lower()
    # Caso típico: el usuario copió la URL ya redirigida al login
    # (facebook.com/login/?next=...story.php...share/v/...)
    if "facebook.com/login" in lower_url:
        return (
            "Pegaste la página de login de Facebook, no el video. "
            "Copia el enlace original del video (facebook.com/watch o /share/v/...) "
            "y asegúrate de que sea público."
        )
    if "facebook" in lower_url and (
        "login" in msg or "unsupported url" in msg or "cookies" in msg
    ):
        return (
            "Facebook exige iniciar sesión para ese video. "
            "Solo funcionan videos públicos: abre el video sin sesión "
            "y copia ese enlace (watch o share)."
        )
    if "login" in msg or "log in" in msg or "cookies" in msg:
        return "Ese contenido requiere iniciar sesión. Usa un enlace público."
    if "private" in msg:
        return "Ese video es privado. Solo funcionan enlaces públicos."
    if "unsupported url" in msg:
        return (
            "Ese enlace no es un video directo (parece una página de login o redirección). "
            "Pega el enlace original del video."
        )
    return f"No pudimos leer ese enlace: {e}"


def _extract_info(url: str, use_cache: bool = True) -> dict:
    """Extrae metadata sin descargar. Si es playlist, toma el primer item."""
    if use_cache:
        with _info_cache_lock:
            hit = _info_cache.get(url)
            if hit and (time.time() - hit[0] < INFO_CACHE_TTL):
                return hit[1]
    opts = {**_base_opts(), "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore
            data = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=_friendly_extract_error(url, e)) from e
    if data is None:
        raise HTTPException(status_code=422, detail="No se encontró contenido en ese enlace.")
    if data.get("_type") == "playlist":
        entries = [e for e in (data.get("entries") or []) if e]
        if not entries:
            raise HTTPException(status_code=422, detail="Esa playlist no tiene videos accesibles.")
        data = entries[0]
    if use_cache:
        with _info_cache_lock:
            _info_cache[url] = (time.time(), data)
            # Evita crecimiento sin control: máximo 200 entradas
            if len(_info_cache) > 200:
                oldest = min(_info_cache, key=lambda k: _info_cache[k][0])
                _info_cache.pop(oldest, None)
    return data


def _qualities_for(data: dict) -> tuple[list[dict], Optional[int]]:
    """Calidades de video disponibles (alturas reales) + tamaño aprox de audio."""
    formats = [f for f in (data.get("formats") or []) if f.get("url")]
    video_heights: dict[int, Optional[int]] = {}
    for f in formats:
        if f.get("vcodec") in (None, "none"):
            continue
        h = f.get("height")
        if not h:
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if h not in video_heights or (size and (not video_heights[h] or size < video_heights[h])):
            video_heights[h] = size
    labels = {2160: "4K 2160p", 1440: "QHD 1440p", 1080: "Full HD 1080p",
              720: "HD 720p", 480: "480p", 360: "360p", 240: "240p", 144: "144p"}
    qualities = [{"id": "best", "label": "La mejor", "size": None, "sizeLabel": "máxima calidad"}]
    for h in sorted(video_heights, reverse=True)[:7]:
        qualities.append({
            "id": str(h),
            "label": labels.get(h, f"{h}p"),
            "size": video_heights[h],
            "sizeLabel": ("≈ " + _human_size(video_heights[h])) if video_heights[h] else "tamaño desconocido",
        })
    audio_size = None
    for f in formats:
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none"):
            s = f.get("filesize") or f.get("filesize_approx")
            if s and (not audio_size or s > audio_size):
                audio_size = s
    return qualities, audio_size


def _format_for(kind: str, quality: str) -> str:
    if kind == "audio":
        return "bestaudio/best"
    if quality in ("best", "", None):
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    try:
        h = int(quality)
    except (TypeError, ValueError):
        h = 720
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
        f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
    )


def _public_job(job: dict) -> dict:
    return {
        "id": job["id"],
        "url": job["url"],
        "kind": job["kind"],
        "quality": job["quality"],
        "status": job["status"],  # queued | downloading | ready | error
        "progress": job.get("progress"),  # 0-100 o None si es indeterminado
        "speed": job.get("speed"),
        "speedLabel": _human_size(job.get("speed")) + "/s" if job.get("speed") else "",
        "eta": job.get("eta"),
        "title": job.get("title"),
        "thumbnail": job.get("thumbnail"),
        "uploader": job.get("uploader"),
        "durationLabel": job.get("durationLabel"),
        "filename": job.get("filename"),
        "size": job.get("size"),
        "sizeLabel": _human_size(job.get("size")) if job.get("size") else "",
        "error": job.get("error"),
    }


def _run_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        url, kind, quality = job["url"], job["kind"], job["quality"]

    # Cola: espera turno si hay demasiadas descargas a la vez (sigue en "queued")
    _job_slots.acquire()
    try:
        with jobs_lock:
            j = jobs.get(job_id)
            if not j:
                return
            j["status"] = "downloading"

        jobdir = TMPDIR / job_id
        jobdir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(jobdir / "%(title).80s.%(ext)s")

        def hook(d: dict):
            if d.get("status") != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            with jobs_lock:
                j = jobs.get(job_id)
                if not j:
                    return
                j["progress"] = round(done / total * 100, 1) if total else None
                j["speed"] = d.get("speed")
                j["eta"] = d.get("eta")

        try:
            data = _extract_info(url)
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.update(
                        title=data.get("title") or "Sin título",
                        thumbnail=data.get("thumbnail"),
                        uploader=data.get("uploader") or data.get("channel") or "Desconocido",
                        durationLabel=_format_duration(data.get("duration")),
                    )

            if kind == "audio":
                ydl_opts: dict = {
                    **_base_opts(),
                    "format": _format_for(kind, quality),
                    "outtmpl": outtmpl,
                    "progress_hooks": [hook],
                    "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
                    "postprocessor_args": ["-ar", "44100"],
                    "prefer_ffmpeg": True,
                }
                ext = "mp3"
            else:
                ydl_opts = {
                    **_base_opts(),
                    "format": _format_for(kind, quality),
                    "outtmpl": outtmpl,
                    "progress_hooks": [hook],
                    "merge_output_format": "mp4",
                }
                ext = "mp4"

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore
                info = ydl.extract_info(url, download=True)
                downloaded = Path(ydl.prepare_filename(info)).with_suffix(f".{ext}")
                if not downloaded.exists():
                    candidates = sorted(jobdir.glob("*"), key=lambda p: p.stat().st_mtime)
                    if not candidates:
                        raise FileNotFoundError("No se generó el archivo.")
                    downloaded = candidates[-1]

            raw_title = (info.get("title") if isinstance(info, dict) else None) or "descarga"
            safe = re.sub(r"[^\w\s\-áéíóúñü]", "", raw_title, flags=re.IGNORECASE).strip()[:60] or "descarga"
            filename = f"{safe}.{downloaded.suffix.lstrip('.') or ext}"
            size = downloaded.stat().st_size
            if size > MAX_FILE_BYTES:
                try:
                    downloaded.unlink()
                except OSError:
                    pass
                raise HTTPException(
                    status_code=413,
                    detail=f"Ese archivo pesa {_human_size(size)} y supera el límite de {MAX_FILE_MB} MB.",
                )
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.update(status="ready", progress=100, filename=filename,
                             path=str(downloaded), size=size)
        except HTTPException as e:
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.update(status="error", error=e.detail)
        except Exception as e:  # noqa: BLE001
            with jobs_lock:
                j = jobs.get(job_id)
                if j:
                    j.update(status="error", error=f"No se pudo descargar: {e}")
    finally:
        _job_slots.release()


def _remove_job_files(job_id: str, path: Optional[str]):
    try:
        if path:
            p = Path(path)
            if p.exists():
                p.unlink()
        d = TMPDIR / job_id
        if d.exists():
            for f in d.iterdir():
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                d.rmdir()
            except OSError:
                pass
    except OSError:
        pass


def _cleanup_loop():
    """Borra cada 5 min los trabajos terminados de más de JOB_TTL_SEC y su /tmp."""
    while True:
        time.sleep(300)
        now = time.time()
        expired: list[tuple[str, Optional[str]]] = []
        with jobs_lock:
            for jid, j in list(jobs.items()):
                if j.get("status") in ("ready", "error") and now - j.get("created_at", now) > JOB_TTL_SEC:
                    expired.append((jid, j.get("path")))
                    jobs.pop(jid, None)
            known = set(jobs)
        for jid, path in expired:
            _remove_job_files(jid, path)
        # Limpia carpetas huérfanas (p. ej. /api/download legacy) más viejas que el TTL
        try:
            for d in TMPDIR.iterdir():
                if not d.is_dir() or d.name in known:
                    continue
                try:
                    if now - d.stat().st_mtime > JOB_TTL_SEC:
                        for f in d.iterdir():
                            try:
                                f.unlink()
                            except OSError:
                                pass
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                except OSError:
                    pass
        except OSError:
            pass


_cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
_cleanup_thread.start()


@app.get("/api/health")
def health():
    with jobs_lock:
        active = sum(1 for j in jobs.values() if j.get("status") in ("queued", "downloading"))
        total = len(jobs)
    try:
        free_mb = round(shutil.disk_usage(TMPDIR).free / 1024 / 1024)
    except OSError:
        free_mb = None
    return {
        "ok": True,
        "yt_dlp": yt_dlp is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "jobs": total,
        "activeJobs": active,
        "tmpFreeMB": free_mb,
        "maxFileMB": MAX_FILE_MB,
    }


@app.post("/api/info")
def get_info(body: InfoRequest):
    url = _check_url(body.url)
    data = _extract_info(url)
    qualities, audio_size = _qualities_for(data)
    return {
        "title": data.get("title") or "Sin título",
        "uploader": data.get("uploader") or data.get("channel") or "Desconocido",
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
        "durationLabel": _format_duration(data.get("duration")),
        "webpage": data.get("webpage_url") or url,
        "extractor": data.get("extractor_key") or data.get("extractor"),
        "qualities": qualities,
        "audioSize": audio_size,
        "audioSizeLabel": ("≈ " + _human_size(audio_size)) if audio_size else "",
    }


@app.post("/api/jobs", status_code=201)
def create_job(body: JobRequest):
    url = _check_url(body.url)
    kind = body.kind if body.kind in ("video", "audio") else "video"
    quality = (body.quality or "best").strip()[:10]
    job_id = uuid.uuid4().hex[:10]
    with jobs_lock:
        # Tope de trabajos: expulsa el más viejo terminado para no crecer sin control
        if len(jobs) >= MAX_JOBS:
            done = [j for j in jobs.values() if j.get("status") in ("ready", "error")]
            victim = min(done or list(jobs.values()), key=lambda j: j.get("created_at", 0))
            jobs.pop(victim["id"], None)
        jobs[job_id] = {
            "id": job_id, "url": url, "kind": kind, "quality": quality,
            "status": "queued", "progress": 0, "speed": None, "eta": None,
            "title": None, "thumbnail": None, "uploader": None,
            "durationLabel": None, "filename": None, "path": None,
            "size": None, "error": None, "created_at": time.time(),
        }
        job = jobs[job_id]
    t = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    t.start()
    return _public_job(job)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
        return _public_job(job)


@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
        if job["status"] != "ready" or not job.get("path"):
            raise HTTPException(status_code=409, detail="El archivo aún no está listo.")
        path, filename = job["path"], job["filename"]
    media = "audio/mpeg" if Path(path).suffix == ".mp3" else "video/mp4"
    return FileResponse(path=path, media_type=media, filename=filename)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    with jobs_lock:
        job = jobs.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado.")
    _remove_job_files(job_id, job.get("path"))
    return {"ok": True}


@app.get("/api/download")
def download(
    url: str = Query(..., description="Enlace a descargar"),
    kind: str = Query("video", pattern="^(video|audio)$"),
    quality: str = Query("best"),
):
    """Descarga directa legacy (sin progreso). Preferir /api/jobs."""
    url = _check_url(url)
    job_id = uuid.uuid4().hex[:10]
    jobdir = TMPDIR / job_id
    jobdir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(jobdir / "%(title).80s.%(ext)s")

    if kind == "audio":
        ydl_opts: dict = {
            **_base_opts(),
            "format": _format_for(kind, quality),
            "outtmpl": outtmpl,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "postprocessor_args": ["-ar", "44100"],
            "prefer_ffmpeg": True,
        }
        media_type, ext = "audio/mpeg", "mp3"
    else:
        ydl_opts = {
            **_base_opts(),
            "format": _format_for(kind, quality),
            "outtmpl": outtmpl,
            "merge_output_format": "mp4",
        }
        media_type, ext = "video/mp4", "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore
            info = ydl.extract_info(url, download=True)
            downloaded = Path(ydl.prepare_filename(info)).with_suffix(f".{ext}")
            if not downloaded.exists():
                candidates = sorted(jobdir.glob("*"), key=lambda p: p.stat().st_mtime)
                if not candidates:
                    raise FileNotFoundError("No se generó el archivo.")
                downloaded = candidates[-1]
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"No se pudo descargar: {e}") from e

    raw_title = (info.get("title") if isinstance(info, dict) else None) or "descarga"
    safe = re.sub(r"[^\w\s\-áéíóúñü]", "", raw_title, flags=re.IGNORECASE).strip()[:60] or "descarga"
    if downloaded.stat().st_size > MAX_FILE_BYTES:
        size = downloaded.stat().st_size
        try:
            downloaded.unlink()
        except OSError:
            pass
        raise HTTPException(
            status_code=413,
            detail=f"Ese archivo pesa {_human_size(size)} y supera el límite de {MAX_FILE_MB} MB.",
        )
    return FileResponse(path=str(downloaded), media_type=media_type,
                        filename=f"{safe}.{downloaded.suffix.lstrip('.') or ext}")
