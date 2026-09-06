# Usar una versión ligera de Python
FROM python:3.10-slim

# Instalar FFmpeg (CRUCIAL para la máxima calidad)
RUN apt-get update && apt-get install -y ffmpeg

# Crear una carpeta para la app
WORKDIR /app

# Copiar e instalar las librerías
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto de tu código (main.py)
COPY . .

# Iniciar el servidor en el puerto 10000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
