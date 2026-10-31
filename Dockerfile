FROM python:3.11-slim

WORKDIR /srv

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml ./
# Install deps by resolving from pyproject (editable-less runtime image).
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.32" "pydantic>=2.9" "pydantic-settings>=2.6" \
    "python-multipart>=0.0.12" "pyjwt>=2.9" "passlib[bcrypt]>=1.7" \
    "langgraph>=0.2.50" "langgraph-checkpoint-postgres>=2.0" "langchain-core>=0.3" "openai>=1.54" \
    "sqlalchemy>=2.0" "psycopg[binary,pool]>=3.2" "redis>=5.2" "minio>=7.2" "cryptography>=43.0" \
    "opentelemetry-sdk>=1.28" "opentelemetry-exporter-otlp>=1.28" \
    "opentelemetry-instrumentation-fastapi>=0.49b0" "opentelemetry-instrumentation-sqlalchemy>=0.49b0" \
    "opentelemetry-instrumentation-redis>=0.49b0" "prometheus-client>=0.21" "python-json-logger>=2.0"

COPY app ./app
COPY scripts ./scripts

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
