import os
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

APP_NAME = "Motor de Descargas Ultra"
PUBLIC_BASE_URL = os.getenv(
    "PUBLIC_BASE_URL",
    "https://motor-descargas.onrender.com",
).rstrip("/")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Render Free tiene almacenamiento efímero: los archivos se conservan poco tiempo.
TEMP_FILE_TTL_MINUTES = int(os.getenv("TEMP_FILE_TTL_MINUTES", "20"))

# 0 = sin límite. La prioridad es máxima calidad disponible.
MAX_HEIGHT = int(os.getenv("MAX_HEIGHT", "0"))

IMAGE_EXTENSIONS = {
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "avif"
}

BROWSER_USER_AGENT = os.getenv(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36",
)

app = FastAPI(
    title=APP_NAME,
    version="3.0.0",
)


def limpiar_temporales_antiguos() -> None:
    limite = __import__("time").time() - TEMP_FILE_TTL_MINUTES * 60

    for archivo in DOWNLOAD_DIR.iterdir():
        try:
            if archivo.is_file() and archivo.stat().st_mtime < limite:
                archivo.unlink(missing_ok=True)
        except OSError:
            pass


def calcular_peso(ruta: Path) -> str:
    try:
        return f"{ruta.stat().st_size / (1024 * 1024):.2f} MB"
    except (OSError, FileNotFoundError):
        return "Desconocido"


def titulo_seguro(valor: str | None) -> str:
    texto = (valor or "archivo_descargado").strip()
    texto = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", texto)
    texto = re.sub(r"\s+", " ", texto).strip(" ._")
    return (texto or "archivo_descargado")[:100]


def es_manifest(url: str) -> bool:
    path = str(url or "").lower().split("?", 1)[0]
    return path.endswith(".m3u8") or path.endswith(".mpd")


def es_imagen(info: dict) -> bool:
    ext = str(info.get("ext") or "").lower()
    if ext in IMAGE_EXTENSIONS:
        return True

    if str(info.get("video_ext") or "").lower() in {"", "none"}:
        vcodec = str(info.get("vcodec") or "").lower()
        acodec = str(info.get("acodec") or "").lower()
        if vcodec in {"", "none"} and acodec in {"", "none"}:
            return bool(info.get("url") or info.get("thumbnails"))

    return False


def extension_imagen(info: dict) -> str:
    ext = str(
        info.get("ext")
        or info.get("image_ext")
        or info.get("thumbnail_ext")
        or ""
    ).lower()

    if ext in IMAGE_EXTENSIONS:
        return ext

    direct = str(info.get("url") or "").lower()
    for candidate in IMAGE_EXTENSIONS:
        if f".{candidate}" in direct:
            return candidate

    return "jpg"


def navegador_headers(url_fuente: str, info: dict | None = None) -> dict:
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept-Language": "es-419,es;q=0.9,en;q=0.8",
    }

    raw = (info or {}).get("http_headers") or {}
    for key, value in raw.items():
        if key.lower() in {
            "user-agent",
            "referer",
            "accept",
            "accept-language",
        } and value:
            headers[key] = str(value)

    host = urlparse(url_fuente).netloc.lower()
    if "instagram." in host:
        headers.setdefault("Referer", "https://www.instagram.com/")
    elif "facebook." in host or host.startswith("fb."):
        headers.setdefault("Referer", "https://www.facebook.com/")
    else:
        headers.setdefault(
            "Referer",
            f"{urlparse(url_fuente).scheme}://{urlparse(url_fuente).netloc}/",
        )

    return headers


def opciones_ytdlp(url_fuente: str, output: str | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "retries": 4,
        "fragment_retries": 4,
        "file_access_retries": 4,
        "socket_timeout": 40,
        "http_headers": navegador_headers(url_fuente),
        "geo_bypass": True,
    }

    if output:
        opts["outtmpl"] = output

    return opts


def clasificar_error(exc: Exception) -> tuple[int, str]:
    texto = str(exc).strip()
    t = texto.lower()

    if any(x in t for x in (
        "private", "login required", "sign in", "members only",
        "authentication required", "age-restricted",
    )):
        return 403, "El contenido es privado, restringido o requiere iniciar sesión."

    if any(x in t for x in (
        "video unavailable", "not available", "removed", "deleted",
        "does not exist", "page not found",
    )):
        return 404, "El contenido no existe, fue eliminado o ya no está disponible."

    if any(x in t for x in (
        "unsupported url", "no suitable extractor",
    )):
        return 422, "El enlace o la plataforma no son compatibles con yt-dlp."

    if "ffmpeg" in t or "postprocess" in t or "merger" in t:
        return 422, "El servidor no pudo fusionar las pistas multimedia con FFmpeg."

    return 500, f"No se pudo procesar el enlace: {texto or 'error desconocido'}"


def seleccionar_directo(info: dict) -> dict | None:
    """
    Devuelve solo un formato HTTP progresivo con vídeo + audio.
    Nunca devuelve HLS/DASH ni una pista de vídeo sin audio.
    """
    formatos = info.get("formats") or []

    candidatos: list[dict] = []

    for fmt in formatos:
        protocol = str(fmt.get("protocol") or "").lower()
        direct_url = str(fmt.get("url") or "")
        vcodec = str(fmt.get("vcodec") or "none").lower()
        acodec = str(fmt.get("acodec") or "none").lower()

        if protocol not in {"http", "https"}:
            continue
        if not direct_url or es_manifest(direct_url):
            continue
        if vcodec in {"", "none"} or acodec in {"", "none"}:
            continue

        height = int(fmt.get("height") or 0)
        if MAX_HEIGHT > 0 and height > MAX_HEIGHT:
            continue

        candidatos.append(fmt)

    if not candidatos:
        # Algunos extractores entregan un único formato fuera de formats[].
        direct_url = str(info.get("url") or "")
        protocol = str(info.get("protocol") or "").lower()
        vcodec = str(info.get("vcodec") or "none").lower()
        acodec = str(info.get("acodec") or "none").lower()

        if (
            direct_url
            and not es_manifest(direct_url)
            and protocol in {"http", "https"}
            and vcodec not in {"", "none"}
            and acodec not in {"", "none"}
        ):
            return info

        return None

    candidatos.sort(
        key=lambda f: (
            int(f.get("height") or 0),
            float(f.get("fps") or 0),
            float(f.get("tbr") or 0),
            int(f.get("filesize") or f.get("filesize_approx") or 0),
        ),
        reverse=True,
    )
    return candidatos[0]


def buscar_archivo(file_id: str) -> Path | None:
    archivos = sorted(
        DOWNLOAD_DIR.glob(f"video_{file_id}.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for archivo in archivos:
        try:
            if archivo.is_file() and archivo.stat().st_size > 0:
                return archivo
        except OSError:
            pass
    return None


def descargar_video_y_fusionar(url_fuente: str, file_id: str) -> Path:
    if not shutil.which("ffmpeg"):
        raise HTTPException(
            status_code=422,
            detail="FFmpeg no está instalado o no está disponible en Render.",
        )

    output_template = str(DOWNLOAD_DIR / f"video_{file_id}.%(ext)s")

    if MAX_HEIGHT > 0:
        selector = (
            f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/"
            f"best[height<={MAX_HEIGHT}]/best"
        )
    else:
        selector = "bestvideo+bestaudio/best"

    opts = opciones_ytdlp(url_fuente, output_template)
    opts.update({
        "format": selector,
        "merge_output_format": "mp4",
        "format_sort": "res,fps,br,filesize",
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }
        ],
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url_fuente, download=True)
    except Exception as exc:
        status, message = clasificar_error(exc)
        raise HTTPException(status_code=status, detail=message) from exc

    archivo = buscar_archivo(file_id)
    if archivo is None:
        raise HTTPException(
            status_code=422,
            detail="yt-dlp terminó sin producir un archivo de vídeo válido.",
        )

    # Si por alguna razón quedó otro contenedor, intentamos localizar MP4.
    mp4 = DOWNLOAD_DIR / f"video_{file_id}.mp4"
    if mp4.exists() and mp4.stat().st_size > 0:
        return mp4

    return archivo


@app.get("/")
def inicio():
    return {
        "ok": True,
        "mensaje": "Motor de Descargas Ultra activo.",
        "servidor": PUBLIC_BASE_URL,
        "version_api": "3.0.0",
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "yt_dlp": yt_dlp.version.__version__,
        "ffmpeg": shutil.which("ffmpeg") or "no encontrado",
        "ffprobe": shutil.which("ffprobe") or "no encontrado",
    }


@app.get("/extraer")
def extraer_contenido(url: str):
    url = (url or "").strip()

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Debes proporcionar una URL.",
        )

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="La URL debe comenzar con http:// o https:// y ser válida.",
        )

    limpiar_temporales_antiguos()

    file_id = uuid.uuid4().hex
    outtmpl = str(DOWNLOAD_DIR / f"video_{file_id}.%(ext)s")

    try:
        # PASO 1: análisis. Importante: no asumimos que info["formats"] exista.
        extract_opts = opciones_ytdlp(url)
        extract_opts["skip_download"] = True

        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise HTTPException(
                status_code=422,
                detail="yt-dlp no pudo obtener información de este enlace.",
            )

        # Un post con varias entradas: usamos la primera descargable.
        if info.get("_type") in {"playlist", "multi_video"}:
            entries = [
                item for item in (info.get("entries") or [])
                if item
            ]
            if not entries:
                raise HTTPException(
                    status_code=422,
                    detail="El enlace no contiene contenido descargable.",
                )
            info = entries[0]

        titulo = titulo_seguro(info.get("title"))

        # ---------------------------------------------------------------
        # IMAGEN
        # ---------------------------------------------------------------
        if es_imagen(info):
            url_imagen = str(info.get("url") or "")

            if not url_imagen or es_manifest(url_imagen):
                for thumb in reversed(info.get("thumbnails") or []):
                    candidate = str((thumb or {}).get("url") or "")
                    if candidate and not es_manifest(candidate):
                        url_imagen = candidate
                        break

            if not url_imagen:
                raise HTTPException(
                    status_code=422,
                    detail="Se detectó una imagen, pero no se obtuvo una URL utilizable.",
                )

            ext = extension_imagen(info)
            headers = navegador_headers(url, info)

            # Compatibilidad con el cliente nuevo y el cliente antiguo.
            formato_imagen = {
                "etiqueta": "Imagen original",
                "ext": ext,
                "peso": "Desconocido",
                "url": url_imagen,
                "altura": info.get("height"),
                "ancho": info.get("width"),
            }

            return {
                "exito": True,
                "es_foto": True,
                "modo": "directo",
                "titulo": titulo,
                "extension": ext,
                "url_directa": url_imagen,
                "headers": headers,
                "formatos": [formato_imagen],
            }

        # ---------------------------------------------------------------
        # VÍDEO DIRECTO CON AUDIO
        # ---------------------------------------------------------------
        directo = seleccionar_directo(info)

        if directo:
            direct_url = str(directo.get("url") or "")
            headers = navegador_headers(url, {
                "http_headers": directo.get("http_headers") or info.get("http_headers") or {}
            })

            filesize = (
                directo.get("filesize")
                or directo.get("filesize_approx")
            )
            peso = (
                f"{int(filesize) / (1024 * 1024):.2f} MB"
                if filesize
                else "Desconocido"
            )

            formato = {
                "id": str(directo.get("format_id") or "direct"),
                "etiqueta": (
                    f"Máxima calidad ({directo.get('height')}p)"
                    if directo.get("height")
                    else "Máxima calidad"
                ),
                "ext": "mp4",
                "peso": peso,
                "url": direct_url,
                "altura": directo.get("height"),
                "ancho": directo.get("width"),
                "fps": directo.get("fps"),
                "has_audio": True,
            }

            return {
                "exito": True,
                "es_foto": False,
                "modo": "directo",
                "titulo": titulo,
                "peso": peso,
                "extension": "mp4",
                "calidad_maxima": {
                    "altura": directo.get("height"),
                    "ancho": directo.get("width"),
                    "fps": directo.get("fps"),
                },
                "url_directa": direct_url,
                "headers": headers,
                # Compatibilidad con la versión antigua de Flutter.
                "formatos": [formato],
            }

        # ---------------------------------------------------------------
        # VÍDEO ADAPTATIVO: vídeo + audio separados
        # ---------------------------------------------------------------
        # Aquí NO devolvemos "no se encontraron formatos".
        # Descargamos ambos flujos en Render y FFmpeg produce un MP4 con audio.
        archivo = descargar_video_y_fusionar(url, file_id)
        peso = calcular_peso(archivo)

        info_final = {
            "altura": info.get("height"),
            "ancho": info.get("width"),
            "fps": info.get("fps"),
        }

        url_servidor = f"{PUBLIC_BASE_URL}/descargar_archivo/{archivo.name}"

        formato_servidor = {
            "id": "server-max",
            "etiqueta": (
                f"Máxima calidad ({info_final['altura']}p)"
                if info_final["altura"]
                else "Máxima calidad + audio"
            ),
            "ext": "mp4",
            "peso": peso,
            "url": url_servidor,
            "altura": info_final["altura"],
            "ancho": info_final["ancho"],
            "fps": info_final["fps"],
            "has_audio": True,
        }

        return {
            "exito": True,
            "es_foto": False,
            "modo": "servidor",
            "titulo": titulo,
            "peso": peso,
            "extension": "mp4",
            "calidad_maxima": info_final,
            "url_descarga": url_servidor,
            # Compatibilidad con el cliente antiguo.
            "formatos": [formato_servidor],
        }

    except HTTPException:
        raise
    except Exception as exc:
        status, message = clasificar_error(exc)
        raise HTTPException(status_code=status, detail=message) from exc


@app.get("/descargar_archivo/{nombre_archivo}")
def servir_archivo(nombre_archivo: str):
    seguro = Path(nombre_archivo).name

    if seguro != nombre_archivo or not seguro.startswith("video_"):
        raise HTTPException(
            status_code=400,
            detail="Nombre de archivo no válido.",
        )

    ruta = DOWNLOAD_DIR / seguro

    if not ruta.is_file():
        raise HTTPException(
            status_code=404,
            detail="El archivo ya no se encuentra en el servidor.",
        )

    return FileResponse(
        path=str(ruta),
        media_type="video/mp4",
        filename=seguro,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{seguro}"',
        },
    )
