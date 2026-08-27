FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DATA_DIR=/data

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser README.md DECISIONS.md pyproject.toml ./

USER appuser

ENTRYPOINT ["python", "-m", "repo_detective"]
CMD ["--help"]

