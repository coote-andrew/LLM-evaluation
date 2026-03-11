# LLM Evaluation Workbench - Docker image
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    tar \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Collect static files at build time
RUN python manage.py collectstatic --noinput

# OpenShift runs containers with arbitrary UIDs in group 0.
# Make runtime-written directories group-writable and owned by root group.
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/data /app/staticfiles \
    && chown -R 1001:0 /app \
    && chmod -R g=u /app

USER 1001

ENV PYTHONUNBUFFERED=1
ENV DJANGO_SETTINGS_MODULE=config.settings

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
