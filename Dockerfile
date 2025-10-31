FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY strictmode ./strictmode
COPY docs ./docs

RUN pip install --no-cache-dir .

ENTRYPOINT ["strictmode"]
