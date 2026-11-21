# Usar Python 3.12 como base
FROM python:3.12-slim

# Instalar uv
RUN pip install uv

# Establecer el directorio de trabajo
WORKDIR /app

# Copiar los archivos de configuración e instalar dependencias de producción
# (sin el grupo dev: pytest/coverage no tienen por qué viajar a producción).
# --locked hace que el build falle en vez de re-resolver silenciosamente si
# pyproject.toml y uv.lock llegaran a desincronizarse.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

# Los navegadores de Playwright viven en una ruta fija y legible por el
# usuario no root que se crea más abajo. --with-deps instala también las
# librerías de sistema que Chromium necesita (funciona igual en amd64/arm64,
# a diferencia de listar paquetes apt a mano).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv run playwright install --with-deps chromium

# Copiar el resto del código
COPY . .

# Metadata de build — expuesto como env var para que bot.py pueda mostrarlo
# en /health sin depender de que el contenedor tenga un .git montado.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

# Usuario no root — los contenedores de este proyecto no necesitan (ni deben
# tener) privilegios de root en tiempo de ejecución.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app /ms-playwright

USER appuser

# Llama directo al Python del venv ya sincronizado — evita que el arranque
# re-resuelva/reinstale dependencias en cada `docker compose up`.
CMD ["/app/.venv/bin/python", "bot.py"]
