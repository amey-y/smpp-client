# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.title="smpp-client" \
      org.opencontainers.image.description="Web-GUI SMPP 3.4 client and load tester with a built-in mock SMSC"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SMPPCLIENT_DATA=/data \
    SMPPCLIENT_HOST=0.0.0.0 \
    SMPPCLIENT_PORT=8095

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY smppclient ./smppclient
COPY static ./static

RUN useradd --uid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin smpp \
 && mkdir -p /data && chown smpp:smpp /data
USER smpp
VOLUME /data

# 8095: web GUI / API; 2775: built-in mock SMSC (only if you start it)
EXPOSE 8095 2775

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('SMPPCLIENT_PORT', '8095'), timeout=2)" || exit 1

ENTRYPOINT ["python", "-m", "smppclient"]
