FROM python:3.12-alpine

RUN apk add --no-cache docker-cli docker-cli-compose

WORKDIR /app
COPY server.py /app/server.py

ENV UPDATER_HOST=0.0.0.0 \
    UPDATER_PORT=8090

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=3)" || exit 1

CMD ["python3", "/app/server.py"]
