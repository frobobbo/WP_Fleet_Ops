FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WP_FLEET_OPS_DATA_DIR=/data \
    PORT=8000

WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app && mkdir -p /data && chown -R app:app /data
COPY pyproject.toml README.md ./
COPY wp_fleet_ops ./wp_fleet_ops
COPY templates ./templates
RUN python -m pip install --no-cache-dir --upgrade pip && python -m pip install --no-cache-dir .
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"
CMD ["uvicorn", "wp_fleet_ops.main:app", "--host", "0.0.0.0", "--port", "8000"]
