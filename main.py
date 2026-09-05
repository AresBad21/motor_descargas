from fastapi import FastAPI, HTTPException
import yt_dlp

app = FastAPI(title="Motor de Descargas Universal en la Nube")

def calcular_peso(bytes_size):
    if not bytes_size:
        return "Desconocido"
    mb = bytes_size / (1024 * 1024)
    return f"{mb:.2f} MB"

@app.get("/")
def inicio():
    return {"mensaje": "¡El motor en la nube está 100% activo!"}

@app.get("/extraer")
def extraer_enlace(url: str):
    if not url:
        raise HTTPException(status_code=400, detail="Debes proporcionar una URL")
    
    ydl_opts = {
        'quiet': True, 
        'nocheckcertificate': True
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formatos_filtrados = []
            
            # 1. Detección de fotos
            es_imagen = info.get('ext') in ['jpg', 'png', 'jpeg', 'webp'] or info.get('vcodec') == 'images'
            
            if es_imagen:
                url_img = info.get('url')
                if info.get('thumbnails'):
                    url_img = info['thumbnails'][-1].get('url')
                
                formatos_filtrados.append({
                    "etiqueta": "Foto Original (Máxima Calidad)", 
                    "url": url_img, 
                    "ext": "jpg",
                    "peso": "Calidad Full"
                })
            else:
                # 2. Detección de videos
                formatos = info.get('formats', [])
                formatos_ordenados = sorted(formatos, key=lambda x: x.get('height') or 0, reverse=True)
                
                for f in formatos_ordenados:
                    vcodec = f.get('vcodec', 'none')
                    acodec = f.get('acodec', 'none')
                    proto = f.get('protocol', '')
                    enlace_fmt = f.get('url', '')
                    
                    # Descartamos listas de fragmentos DASH o M3U8 que corrompen el archivo local
                    if 'm3u8' in proto or 'dash' in proto or '.mpd' in enlace_fmt:
                        continue
                    
                    # Exigimos video y audio juntos en una sola URL progresiva
                    if vcodec != 'none' and acodec != 'none':
                        alto = f.get('height')
                        if alto:
                            peso_bytes = f.get('filesize') or f.get('filesize_approx')
                            
                            etiqueta = f"Video {alto}p"
                            if alto >= 2160:
                                etiqueta += " (4K ULTRA HD)"
                            elif alto >= 1080:
                                etiqueta += " (FULL HD)"
                            elif alto >= 720:
                                etiqueta += " (HD)"
                            
                            formatos_filtrados.append({
                                "etiqueta": etiqueta, 
                                "url": enlace_fmt, 
                                "ext": f.get('ext', 'mp4'),
                                "peso": calcular_peso(peso_bytes)
                            })

            # Respaldo si no hay formatos combinados limpios
            if not formatos_filtrados and info.get('url'):
                proto_directo = info.get('protocol', '')
                if 'm3u8' not in proto_directo and 'dash' not in proto_directo:
                    peso_bytes = info.get('filesize') or info.get('filesize_approx')
                    formatos_filtrados.append({
                        "etiqueta": "Calidad Estándar Compatible", 
                        "url": info.get('url'), 
                        "ext": info.get('ext', 'mp4'),
                        "peso": calcular_peso(peso_bytes)
                    })
            
            resultados = []
            vistos = set()
            for f in formatos_filtrados:
                if f['etiqueta'] not in vistos:
                    vistos.add(f['etiqueta'])
                    resultados.append(f)

            if not resultados:
                raise HTTPException(status_code=404, detail="No se encontraron formatos de video compatibles para descarga directa.")
                
            return {
                "exito": True,
                "titulo": info.get('title', 'archivo_descargado'),
                "formatos": resultados
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
