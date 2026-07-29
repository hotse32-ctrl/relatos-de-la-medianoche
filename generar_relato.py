"""
Relatos de la Medianoche - Generador automatico de videos de terror
======================================================================
Flujo:
1. Buscar en Drive la carpeta de historia mas antigua dentro de "Pendientes/"
   (cada carpeta = una historia, con imagenes numeradas 1,2,3... y un .docx
   con el texto completo).
2. Leer el texto completo desde el .docx.
3. Narrar el texto completo con edge-tts (voz configurable).
4. Armar el video: las imagenes se muestran en orden numerico, cada una
   ocupando una porcion de tiempo proporcional a la duracion total del
   audio (todas las imagenes se ven de principio a fin, ninguna se corta
   ni se repite), con un crossfade suave entre ellas.
5. Generar titulo/descripcion (Gemini, con respaldo local sin IA).
6. Subir el video a YouTube (publico).
7. Guardar copia en Drive: "Videos Generados/YYYY-MM-DD/".
8. Mover la carpeta de la historia (imagenes + docx) de "Pendientes/" a
   "Subidas/", junto con el video final ya generado.
"""

import os
import re
import json
import random
import datetime
import tempfile
import subprocess
from pathlib import Path

import edge_tts
import asyncio
import docx

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# ---------------------------------------------------------------------------
# Shim de compatibilidad Pillow / moviepy 1.0.3
# ---------------------------------------------------------------------------
from PIL import Image
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

from moviepy.editor import (
    ImageClip,
    AudioFileClip,
    concatenate_audioclips,
    concatenate_videoclips,
    CompositeVideoClip,
)

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

VOZ = "es-US-AlonsoNeural"  # narrador calmado y grave, pensado para relatos largos
SILENCIO_INICIO = 1.0
SILENCIO_FIN = 1.5
TRANSICION = 0.6  # segundos de crossfade entre imagenes

CARPETA_PENDIENTES = "Pendientes"
CARPETA_SUBIDAS = "Subidas"
CARPETA_VIDEOS_GENERADOS = "Videos Generados"

DRIVE_FOLDER_ID_RAIZ = os.environ.get("DRIVE_FOLDER_ID_RAIZ")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

ANCHO, ALTO = 1920, 1080  # formato horizontal, video largo tipo podcast

TMP_DIR = Path(tempfile.mkdtemp(prefix="relatos_medianoche_"))

# Offset horario de Chile respecto a UTC — lección aprendida de El Lado
# Oscuro: nunca usar datetime.now()/date.today() crudo en un runner de
# GitHub Actions (corre en UTC), siempre ajustar a la hora de Chile.
OFFSET_CHILE = datetime.timedelta(hours=-4)


def ahora_chile() -> datetime.datetime:
    return datetime.datetime.utcnow() + OFFSET_CHILE


# ---------------------------------------------------------------------------
# Lectura del texto de la historia (.docx)
# ---------------------------------------------------------------------------

def leer_texto_docx(ruta_docx: Path) -> str:
    documento = docx.Document(str(ruta_docx))
    parrafos = [p.text.strip() for p in documento.paragraphs if p.text.strip()]
    return "\n\n".join(parrafos)


# ---------------------------------------------------------------------------
# Audio (narracion completa con edge-tts)
# ---------------------------------------------------------------------------

async def generar_audio_tts(texto: str, ruta_salida: Path, voz: str = VOZ):
    comunicador = edge_tts.Communicate(texto, voz)
    await comunicador.save(str(ruta_salida))


def _generar_silencio_mp3(ruta_salida: Path, duracion: float):
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        "anullsrc=r=44100:cl=stereo", "-t", str(duracion),
        "-q:a", "9", str(ruta_salida)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def agregar_silencios(ruta_audio_in: Path, ruta_audio_out: Path):
    clip = AudioFileClip(str(ruta_audio_in))
    sil_i = TMP_DIR / "sil_i.mp3"
    sil_f = TMP_DIR / "sil_f.mp3"
    _generar_silencio_mp3(sil_i, SILENCIO_INICIO)
    _generar_silencio_mp3(sil_f, SILENCIO_FIN)
    partes = [AudioFileClip(str(sil_i)), clip, AudioFileClip(str(sil_f))]
    final = concatenate_audioclips(partes)
    final.write_audiofile(str(ruta_audio_out), fps=44100, logger=None)
    for p in partes:
        p.close()
    final.close()


# ---------------------------------------------------------------------------
# Video (imagenes repartidas proporcional a la duracion total del audio)
# ---------------------------------------------------------------------------

def numero_desde_nombre(ruta: Path) -> int:
    m = re.search(r"\d+", ruta.stem)
    return int(m.group()) if m else 0


def ordenar_imagenes(carpeta: Path) -> list:
    extensiones = {".jpg", ".jpeg", ".png", ".webp"}
    imagenes = [p for p in carpeta.iterdir() if p.suffix.lower() in extensiones]
    imagenes.sort(key=numero_desde_nombre)
    return imagenes


def crear_clip_imagen(ruta_imagen: Path, duracion: float) -> ImageClip:
    clip = ImageClip(str(ruta_imagen)).set_duration(duracion)
    clip = clip.resize(height=ALTO)
    if clip.w < ANCHO:
        clip = clip.resize(width=ANCHO)
    clip = clip.crop(x_center=clip.w / 2, y_center=clip.h / 2, width=ANCHO, height=ALTO)
    return clip.crossfadein(min(TRANSICION, duracion / 2))


def construir_video(imagenes: list, duracion_total: float) -> CompositeVideoClip:
    duracion_por_imagen = duracion_total / len(imagenes)
    clips = [crear_clip_imagen(img, duracion_por_imagen) for img in imagenes]
    return concatenate_videoclips(clips, method="compose", padding=-TRANSICION)


# ---------------------------------------------------------------------------
# Titulo y descripcion
# ---------------------------------------------------------------------------

def generar_metadatos(nombre_historia: str, texto: str):
    if GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            modelo = genai.GenerativeModel("gemini-flash-latest")
            prompt = (
                "Genera un titulo corto, impactante y llamativo (estilo canal de "
                "relatos de terror reales en YouTube) para este relato, y una "
                "descripcion de 2-3 frases. Historia:\n\n"
                f"{texto[:2000]}\n\n"
                "Responde en JSON con las claves 'titulo' y 'descripcion'."
            )
            respuesta = modelo.generate_content(prompt)
            texto_resp = re.sub(r"^```json|```$", "", respuesta.text.strip(), flags=re.MULTILINE).strip()
            datos = json.loads(texto_resp)
            if datos.get("titulo"):
                return datos["titulo"], datos.get("descripcion", "")
        except Exception as e:
            print(f"[WARN] Fallback de metadatos (Gemini fallo): {e}")

    titulo = f"{nombre_historia} | Relatos de la Medianoche"
    descripcion = f"{nombre_historia}\n\nUna historia real de terror narrada en Relatos de la Medianoche."
    return titulo, descripcion


# ---------------------------------------------------------------------------
# Google Drive helpers
# ---------------------------------------------------------------------------

def obtener_credenciales_drive():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["DRIVE_REFRESH_TOKEN"],
        client_id=os.environ["DRIVE_CLIENT_ID"],
        client_secret=os.environ["DRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return creds


def obtener_credenciales_youtube():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())
    return creds


def buscar_id_subcarpeta(drive_service, nombre: str, carpeta_padre_id: str):
    query = (
        f"name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{carpeta_padre_id}' in parents and trashed = false"
    )
    resultado = drive_service.files().list(q=query, fields="files(id, name)").execute()
    archivos = resultado.get("files", [])
    if not archivos:
        raise RuntimeError(f"No se encontro la carpeta '{nombre}' dentro de la carpeta raiz.")
    return archivos[0]["id"]


def crear_subcarpeta_si_no_existe(drive_service, nombre: str, carpeta_padre_id: str):
    query = (
        f"name = '{nombre}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{carpeta_padre_id}' in parents and trashed = false"
    )
    resultado = drive_service.files().list(q=query, fields="files(id, name)").execute()
    archivos = resultado.get("files", [])
    if archivos:
        return archivos[0]["id"]
    metadata = {
        "name": nombre,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [carpeta_padre_id],
    }
    carpeta = drive_service.files().create(body=metadata, fields="id").execute()
    return carpeta["id"]


def obtener_historia_mas_antigua(drive_service, carpeta_pendientes_id: str):
    """Devuelve la subcarpeta de historia mas antigua dentro de Pendientes/, o None."""
    query = (
        f"'{carpeta_pendientes_id}' in parents and trashed = false "
        "and mimeType = 'application/vnd.google-apps.folder'"
    )
    resultado = drive_service.files().list(
        q=query, fields="files(id, name, createdTime)", orderBy="createdTime",
    ).execute()
    carpetas = resultado.get("files", [])
    return carpetas[0] if carpetas else None


GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DOCX_EXPORT_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def descargar_carpeta_historia(drive_service, carpeta_id: str, destino_local: Path):
    """Descarga todos los archivos (imagenes + texto) de la carpeta de la historia.

    Acepta el texto de la historia en dos formatos:
    - Un archivo .docx real (subido directamente a Drive).
    - Un Google Doc nativo (creado/editado desde Google Docs) - en este caso se
      exporta automaticamente a .docx antes de guardarlo localmente, para que el
      resto del pipeline (leer_texto_docx) funcione igual en ambos casos.
    """
    destino_local.mkdir(parents=True, exist_ok=True)
    resultado = drive_service.files().list(
        q=f"'{carpeta_id}' in parents and trashed = false",
        fields="files(id, name, mimeType)",
    ).execute()
    archivos = resultado.get("files", [])
    archivos_normalizados = []
    for archivo in archivos:
        nombre = archivo["name"]
        if archivo.get("mimeType") == GOOGLE_DOC_MIME:
            # Google Doc nativo -> exportar como .docx
            if not nombre.lower().endswith(".docx"):
                nombre = nombre + ".docx"
            ruta_local = destino_local / nombre
            request = drive_service.files().export_media(
                fileId=archivo["id"], mimeType=DOCX_EXPORT_MIME
            )
            with open(ruta_local, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                listo = False
                while not listo:
                    _, listo = downloader.next_chunk()
        else:
            ruta_local = destino_local / nombre
            request = drive_service.files().get_media(fileId=archivo["id"])
            with open(ruta_local, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                listo = False
                while not listo:
                    _, listo = downloader.next_chunk()
        archivos_normalizados.append({**archivo, "name": nombre})
    return archivos_normalizados


def subir_archivo_drive(drive_service, ruta_local: Path, carpeta_id: str, nombre: str = None):
    metadata = {"name": nombre or ruta_local.name, "parents": [carpeta_id]}
    media = MediaFileUpload(str(ruta_local), resumable=True)
    archivo = drive_service.files().create(body=metadata, media_body=media, fields="id").execute()
    return archivo["id"]


def mover_carpeta_drive(drive_service, carpeta_id: str, carpeta_origen_id: str, carpeta_destino_id: str):
    """Mueve la carpeta completa (y todo su contenido) de origen a destino."""
    drive_service.files().update(
        fileId=carpeta_id,
        addParents=carpeta_destino_id,
        removeParents=carpeta_origen_id,
        fields="id, parents",
    ).execute()


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def publicar_en_youtube(youtube_service, ruta_video: Path, titulo: str, descripcion: str):
    body = {
        "snippet": {
            "title": titulo[:100],
            "description": descripcion[:5000],
            "tags": ["historias de terror", "relatos de terror", "terror real", "misterio", "relatos de la medianoche"],
            "categoryId": "24",  # Entertainment
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(ruta_video), chunksize=-1, resumable=True, mimetype="video/mp4")
    request = youtube_service.videos().insert(part="snippet,status", body=body, media_body=media)
    respuesta = None
    while respuesta is None:
        _, respuesta = request.next_chunk()
    return respuesta["id"]


# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------

def procesar_una_historia(drive_service, youtube_service, carpeta_pendientes_id,
                           carpeta_subidas_id, carpeta_videos_gen_id) -> bool:
    historia_info = obtener_historia_mas_antigua(drive_service, carpeta_pendientes_id)
    if historia_info is None:
        print("No hay historias pendientes en Drive. Nada que hacer.")
        return False

    nombre_historia = historia_info["name"]
    carpeta_id = historia_info["id"]
    print(f"Historia seleccionada: {nombre_historia}")

    carpeta_local = TMP_DIR / "historia"
    archivos = descargar_carpeta_historia(drive_service, carpeta_id, carpeta_local)

    docx_local = next((carpeta_local / a["name"] for a in archivos if a["name"].lower().endswith(".docx")), None)
    if docx_local is None:
        print(f"[ERROR] La carpeta '{nombre_historia}' no tiene un archivo .docx con el texto. Se omite.")
        return True  # se considera "procesada" para no bloquear el pipeline; revisar manualmente

    texto = leer_texto_docx(docx_local)
    imagenes = ordenar_imagenes(carpeta_local)
    if not imagenes:
        print(f"[ERROR] La carpeta '{nombre_historia}' no tiene imagenes. Se omite.")
        return True

    print(f"Texto: {len(texto)} caracteres | Imagenes: {len(imagenes)}")

    # --- Narracion ---
    ruta_tts_crudo = TMP_DIR / "narracion_cruda.mp3"
    asyncio.run(generar_audio_tts(texto, ruta_tts_crudo))
    ruta_narracion_final = TMP_DIR / "narracion_final.mp3"
    agregar_silencios(ruta_tts_crudo, ruta_narracion_final)

    audio_final = AudioFileClip(str(ruta_narracion_final))
    duracion_total = audio_final.duration

    # --- Video ---
    video_imagenes = construir_video(imagenes, duracion_total)
    video_final = video_imagenes.set_audio(audio_final)

    ruta_video_final = TMP_DIR / f"{nombre_historia}.mp4"
    video_final.write_videofile(
        str(ruta_video_final), fps=30, codec="libx264", audio_codec="aac", logger=None
    )

    # --- Metadatos ---
    titulo, descripcion = generar_metadatos(nombre_historia, texto)
    print(f"Titulo: {titulo}")

    # --- Publicar en YouTube ---
    video_id = publicar_en_youtube(youtube_service, ruta_video_final, titulo, descripcion)
    print(f"Publicado en YouTube: https://youtube.com/watch?v={video_id}")

    # --- Guardar copia en Drive: Videos Generados/YYYY-MM-DD/ ---
    hoy = ahora_chile().date().isoformat()
    carpeta_fecha_id = crear_subcarpeta_si_no_existe(drive_service, hoy, carpeta_videos_gen_id)
    subir_archivo_drive(drive_service, ruta_video_final, carpeta_fecha_id)
    print(f"Copia guardada en Drive: Videos Generados/{hoy}/{ruta_video_final.name}")

    # --- Guardar el video tambien dentro de la carpeta de la historia, y mover todo a Subidas ---
    subir_archivo_drive(drive_service, ruta_video_final, carpeta_id)
    mover_carpeta_drive(drive_service, carpeta_id, carpeta_pendientes_id, carpeta_subidas_id)
    print(f"Carpeta '{nombre_historia}' movida a Subidas/ (con video incluido)")

    return True


def main():
    print("== Relatos de la Medianoche: generador de videos ==")

    creds_drive = obtener_credenciales_drive()
    drive_service = build("drive", "v3", credentials=creds_drive)
    creds_youtube = obtener_credenciales_youtube()
    youtube_service = build("youtube", "v3", credentials=creds_youtube)

    carpeta_pendientes_id = buscar_id_subcarpeta(drive_service, CARPETA_PENDIENTES, DRIVE_FOLDER_ID_RAIZ)
    carpeta_subidas_id = crear_subcarpeta_si_no_existe(drive_service, CARPETA_SUBIDAS, DRIVE_FOLDER_ID_RAIZ)
    carpeta_videos_gen_id = crear_subcarpeta_si_no_existe(drive_service, CARPETA_VIDEOS_GENERADOS, DRIVE_FOLDER_ID_RAIZ)

    procesar_una_historia(
        drive_service, youtube_service,
        carpeta_pendientes_id, carpeta_subidas_id, carpeta_videos_gen_id,
    )

    print("== Proceso completado ==")


if __name__ == "__main__":
    main()
