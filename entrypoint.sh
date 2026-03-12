#!/bin/sh
set -e

echo "--- whoami ---"
whoami
echo "--- /app/data permissions ---"
ls -la /app/
ls -la /app/data/ || echo "data dir not accessible"
echo "--- trying to write ---"
touch /app/data/test.txt && echo "write OK" || echo "write FAILED"

python manage.py migrate --noinput
exec gunicorn config.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
