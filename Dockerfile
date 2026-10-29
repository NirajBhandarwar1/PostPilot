FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Run from /app (not pip-installed) so the data/ and output/ folders resolve next to the code.
RUN pip install --no-cache-dir "pydantic>=2.7" && useradd --create-home app
COPY postpilot ./postpilot
RUN mkdir -p /app/data /app/output && chown -R app /app

USER app
# Settings come from the environment (--env-file .env); state and drafts live on volumes.
VOLUME ["/app/data", "/app/output"]
HEALTHCHECK --interval=5m --timeout=30s CMD python -m postpilot status > /dev/null || exit 1
CMD ["python", "-m", "postpilot", "run"]
