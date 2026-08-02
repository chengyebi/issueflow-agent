FROM mcr.microsoft.com/devcontainers/python:3.12-bullseye AS base

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

FROM base AS test

COPY app ./app
COPY alembic.ini .
COPY migrations ./migrations
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-dev.txt
COPY pyproject.toml .
COPY tests ./tests

CMD ["pytest", "-q"]

FROM base AS runtime

COPY app ./app
COPY alembic.ini .
COPY migrations ./migrations

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
