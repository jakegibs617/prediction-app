FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY app /app/app
COPY sql /app/sql

RUN pip install --no-cache-dir .

CMD ["prediction-app", "run", "research_cycle"]
