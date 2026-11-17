# Build-time networking is used only to install public dependencies. Runtime has none.
FROM docker.io/searxng/searxng@sha256:b36af7984b87191b595bc5301418ed6432c047668a4547ab531a7439b816fac3 AS search
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
COPY --from=search /usr/local/searxng/searx /opt/searxng/searx
COPY docker/isolated/runtime-requirements.txt /opt/runtime-requirements.txt
RUN pip install -r /opt/runtime-requirements.txt
RUN playwright install --with-deps chromium
WORKDIR /opt/web-search
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps .
COPY docker/isolated/settings.yml /opt/settings.yml
RUN useradd --uid 1000 --create-home worker \
    && mkdir -p /data && chown 1000:1000 /data \
    && chmod -R a+rX /opt/browsers
ENV PYTHONPATH=/opt/searxng WEB_SEARCH_ISOLATED_WORKER=1 HOME=/tmp/home \
    WEB_SEARCH_SEARXNG_URL=http://127.0.0.1:8080 WEB_SEARCH_DATA_DIR=/data \
    CRAWL4_AI_BASE_DIRECTORY=/data SEARXNG_SETTINGS_PATH=/opt/settings.yml \
    WEB_SEARCH_ALLOW_PRIVATE_URLS=false WEB_SEARCH_ALLOW_PROXY_FAKE_IPS=false
USER 1000:1000
ENTRYPOINT ["python", "-m", "web_research.isolation.worker"]
