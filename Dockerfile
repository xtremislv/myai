FROM python:3.11-slim

WORKDIR /srv

RUN pip install --no-cache-dir --upgrade pip

# tests/test_ops.py checks this list stays in step with pyproject.toml.
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" "pydantic-settings>=2.6" \
    "python-multipart>=0.0.12" "pyjwt>=2.9" "bcrypt>=4.1" "httpx>=0.27" \
    "langgraph>=1.0" "langgraph-checkpoint-postgres>=2.0" "langchain-core>=0.3" "openai>=1.54" \
    "sqlalchemy>=2.0" "alembic>=1.13" "psycopg[binary,pool]>=3.2" "redis>=5.2" "minio>=7.2" "pypdf>=5.0" "python-docx>=1.1" "jinja2>=3.1" "cryptography>=43.0" \
    "playwright>=1.48" "beautifulsoup4>=4.12" "lxml>=5.0" \
    "opentelemetry-sdk>=1.28" "opentelemetry-exporter-otlp>=1.28" \
    "opentelemetry-instrumentation-fastapi>=0.49b0" "opentelemetry-instrumentation-sqlalchemy>=0.49b0" \
    "opentelemetry-instrumentation-redis>=0.49b0" "prometheus-client>=0.21" "python-json-logger>=2.0"

# Chromium for the agents' browser, in a shared path the non-root user can read.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN playwright install --with-deps chromium

COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --create-home --uid 1000 app && chown -R app /srv
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
