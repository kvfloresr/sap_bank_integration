FROM python:3.12-slim

LABEL maintainer="Comversa SA"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SAP_WEB_SECRET=cambiar-en-produccion \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Instalar ODBC Driver 18 para SQL Server (metodo moderno sin apt-key)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg2 unixodbc-dev ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        | sed 's|signed-by=.*|signed-by=/usr/share/keyrings/microsoft-prod.gpg|' \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar codigo fuente
COPY src/ ./src/
COPY config/ ./config/

# Crear carpetas de datos (se sobreescriben por volumenes)
RUN mkdir -p \
    /data/lth/inbound /data/lth/procesados /data/lth/errores /data/lth/reportes \
    /data/bk/inbound  /data/bk/procesados  /data/bk/errores  /data/bk/reportes

EXPOSE 5000

CMD ["python", "-m", "waitress", "--host=0.0.0.0", "--port=5000", \
    "src.sap_bank.interfaces.web.app:app"]