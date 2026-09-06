import os
import shutil
import uuid
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import yt_dlp


app = FastAPI(
    title="Motor de Descargas Ultra",
    version="2.0.0",
)

# Render usará esta URL pública. No se usa la IP de la PC.
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://motor-descargas.onrender.com",
).rstrip("/")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Los videos son temporales: se eliminan después de entregarlos al celular.
TEMP_FILE_TTL_MINUTES = 30

IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif"}


def calcular_peso(ruta_archivo: Path) -> str:
    try:
        size = ruta_archivo.stat().st_size
    except (FileNotFoundError, OSError):
        return "Desconocido"

    return f"{size / (1024 * 1024):.2f} MB"


def limpiar_temporales_antiguos() -> None:
    """Evita que el almacenamiento temporal de Render se llene."""
    limite = datetime.now().timestamp() - (TEMP_FILE_TTL_MINUTES * 60)

    for archivo in DOWNLOAD_DIR.glob("*.mp4"):
        try:
            if archivo.stat().st_mtime < limite:
                archivo.unlink(missing_ok=True)
        except OSError:
            pass


def eliminar_archivo(ruta: Path) -> None:
    try:
        ruta.unlink(missing_ok=True)
    except OSError:
        pass


def obtener_extension_imagen(info: dict) -> str:
    ext = str(
        info.get("ext")
        or info.get("image_ext")
        or info.get("thumbnail_ext")
        or ""
    ).lower()

    if ext in IMAGE_EXTENSIONS:
        return ext

    url = str(info.get("url") or "").lower()
    for posible in IMAGE_EXTENSIONS:
        if f".{posible}" in url:
            return posible

    return "jpg"


def parece_imagen(info: dict) -> bool:
    ext = str(info.get("ext") or "").lower()
    if ext in IMAGE_EXTENSIONS:
        return True

    video_ext = str(info.get("video_ext") or "").lower()
    if not video_ext and info.get("vcodec") in (None, "none"):
        # Algunos extractores entregan publicaciones de una sola imagen
        # sin un formato de vídeo.
        return bool(info.get("url") and not info.get("formats"))

    return False


def titulo_seguro(titulo: str) -> str:
    limpio = "".join(
        caracter if caracter.isalnum() or caracter in " -_()." else "_"
        for caracter in titulo
    )
    limpio = " ".join(limpio.split()).strip(" ._")
    return (limpio or "video_descargado")[:80]


@app.get("/")
def inicio():
    return {
        "ok": True,
        "mensaje": "Motor de Descargas Ultra activo.",
        "servidor": PUBLIC_BASE_URL,
    }


@app.get("/health")
def health():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    return {
        "ok": True,
        "yt_dlp": True,
        "ffmpeg": ffmpeg or "no encontrado",
        "ffprobe": ffprobe or "no encontrado",
    }


@app.get("/extraer")
def extraer_contenido(url: str):
    url = url.strip()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar una URL.",
        )

    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="La URL debe comenzar con http:// o https://.",
        )

    limpiar_temporales_antiguos()

    file_id = uuid.uuid4().hex
    output_template = str(DOWNLOAD_DIR / f"video_{file_id}.%(ext)s")

    # Primera pasada: solo analiza el enlace.
    # Así una imagen no se descarga inútilmente al servidor.
    extract_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise HTTPException(
                status_code=422,
                detail="No se pudo obtener información del enlace.",
            )

        # Algunas publicaciones pueden venir como playlist/entries.
        # Para una app de un solo enlace, usamos el primer elemento.
        if info.get("_type") in {"playlist", "multi_video"}:
            entries = [entrada for entrada in (info.get("entries") or []) if entrada]
            if not entries:
                raise HTTPException(
                    status_code=422,
                    detail="El enlace no contiene contenido descargable.",
                )
            info = entries[0]

        titulo = titulo_seguro(str(info.get("title") or "archivo_descargado"))

        if parece_imagen(info):
            url_imagen = info.get("url")

            if not url_imagen and info.get("thumbnails"):
                thumbnails = info.get("thumbnails") or []
                for thumbnail in reversed(thumbnails):
                    if thumbnail and thumbnail.get("url"):
                        url_imagen = thumbnail["url"]
                        break

            if not url_imagen:
                raise HTTPException(
                    status_code=422,
                    detail="Se detectó una imagen, pero no se obtuvo su URL directa.",
                )

            return {
                "exito": True,
                "es_foto": True,
                "titulo": titulo,
                "extension": obtener_extension_imagen(info),
                "url_directa": url_imagen,
            }

        # Selección: máxima calidad disponible de vídeo + mejor audio.
        # No limitamos a 1080p: si el sitio ofrece 1440p/4K/8K,
        # yt-dlp puede elegirlo. FFmpeg se encarga de muxearlo a MP4.
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "nocheckcertificate": True,
            "retries": 3,
            "fragment_retries": 3,
            "file_access_retries": 3,
            "format_sort": "res,fps,br,filesize",
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_descarga = ydl.extract_info(url, download=True)

        posibles = sorted(
            DOWNLOAD_DIR.glob(f"video_{file_id}.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        if not posibles:
            raise HTTPException(
                status_code=500,
                detail=(
                    "FFmpeg/yt-dlp no generó el archivo final. "
                    "Comprueba que FFmpeg esté instalado en Render."
                ),
            )

        ruta_final = posibles[0]

        # El resultado final debe ser MP4 para que el teléfono lo reproduzca
        # fácilmente después del mux de vídeo + audio.
        if ruta_final.suffix.lower() != ".mp4":
            candidato_mp4 = ruta_final.with_suffix(".mp4")
            if candidato_mp4.exists():
                ruta_final = candidato_mp4

        nombre_archivo = ruta_final.name
        peso = calcular_peso(ruta_final)

        return {
            "exito": True,
            "es_foto": False,
            "titulo": str(
                info_descarga.get("title")
                or info.get("title")
                or "video_descargado"
            ),
            "peso": peso,
            "calidad_maxima": {
                "altura": info_descarga.get("height") or info.get("height"),
                "ancho": info_descarga.get("width") or info.get("width"),
                "fps": info_descarga.get("fps") or info.get("fps"),
            },
            "url_descarga": (
                f"{PUBLIC_BASE_URL}/descargar_archivo/{nombre_archivo}"
            ),
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"No se pudo procesar el enlace: {exc}",
        ) from exc


@app.get("/descargar_archivo/{nombre_archivo}")
def servir_archivo(nombre_archivo: str):
    # Impide que un nombre recibido por la URL salga de DOWNLOAD_DIR.
    nombre_seguro = Path(nombre_archivo).name

    if nombre_seguro != nombre_archivo or not nombre_seguro.startswith("video_"):
        raise HTTPException(
            status_code=400,
            detail="Nombre de archivo no válido.",
        )

    ruta = DOWNLOAD_DIR / nombre_seguro

    if not ruta.is_file():
        raise HTTPException(
            status_code=404,
            detail="El video ya no se encuentra en el servidor.",
        )

    return FileResponse(
        path=str(ruta),
        media_type="video/mp4",
        filename=nombre_seguro,
        background=BackgroundTask(eliminar_archivo, ruta),
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{nombre_seguro}"',
        },
    )
