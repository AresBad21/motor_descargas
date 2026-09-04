FROM python:3.11-slim

# Instala ffmpeg automáticamente en el servidor de la nube
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Inicia el servidor en el puerto que pide la nube
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
