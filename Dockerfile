# Imagen Docker del chatbot TALENTUM GROUP
FROM python:3.11-slim

WORKDIR /app

# Dependencias primero (mejor caché de capas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código de la aplicación
COPY . .

# Render inyecta $PORT; valor por defecto para uso local
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
