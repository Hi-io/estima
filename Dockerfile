FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /srv
COPY pyproject.toml /srv/pyproject.toml
COPY estima /srv/estima
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home estima

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/healthz',timeout=2)"
CMD ["sh", "-c", "uvicorn estima.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
