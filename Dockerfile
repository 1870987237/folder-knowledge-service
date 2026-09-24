FROM node:22-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml constraints.txt ./
COPY folder_kb ./folder_kb
COPY --from=frontend /build/folder_kb/web ./folder_kb/web
RUN pip install -c constraints.txt . && useradd --uid 10001 --create-home kb \
    && mkdir /service-data && chown 10001:10001 /service-data
USER 10001:10001
ENV KB_ROOT=/knowledge KB_HOST=0.0.0.0 KB_PORT=8790
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz',timeout=2)" || exit 1
CMD ["python", "-m", "folder_kb"]
