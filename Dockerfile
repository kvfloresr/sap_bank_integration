FROM python:3.12-slim

LABEL maintainer="Comversa SA"
LABEL description="SAP Bank Integration Web"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAP_WEB_SECRET=cambiar-en-produccion

WORKDIR /app

# Instalar dependencias (cacheado por Docker si no cambia requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir waitress

# Copiar solo el codigo fuente — sin .venv, sin .git, sin test_local.py
COPY src/ ./src/
COPY config/ ./config/

# Carpetas de datos (se sobreescriben por volumenes en docker-compose)
RUN mkdir -p \
    /data/lth/inbound /data/lth/procesados /data/lth/errores /data/lth/reportes \
    /data/bk/inbound  /data/bk/procesados  /data/bk/errores  /data/bk/reportes

EXPOSE 5000

# Waitress: servidor WSGI de produccion (no el dev server de Flask)
CMD ["python", "-m", "waitress", "--host=0.0.0.0", "--port=5000", \
     "src.sap_bank.interfaces.web.app:app"]