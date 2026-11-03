FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

# Dependency install is cached separately from the source copy.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations
COPY data ./data

EXPOSE 8000

# Postgres deployments should run `alembic upgrade head` before starting.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
