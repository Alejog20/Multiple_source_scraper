# Usar Python 3.12 como base
FROM python:3.12-slim

# Instalar dependencias del sistema de Linux requeridas por Playwright
RUN apt-get update && apt-get install -y \
    wget gnupg libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxext6 \
    libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Instalar uv
RUN pip install uv

# Establecer el directorio de trabajo
WORKDIR /app

# Copiar los archivos de configuración
COPY pyproject.toml uv.lock ./

# Instalar dependencias (sin --frozen para que actualice los cambios que haremos en la web)
RUN uv sync

# Instalar el navegador Chromium
RUN uv run playwright install chromium

# Copiar el resto del código
COPY . .

# Comando para iniciar el bot
CMD ["uv", "run", "python", "bot.py"]
