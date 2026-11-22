#!/bin/bash
set -e
for c in vanguard-backend vanguard-celery-worker vanguard-celery-beat vanguard-telegram-growth-worker; do
  echo "Updating $c..."
  docker cp /opt/vanguard/backend/app/modules/acquisition/simple_growth.py "$c:/app/app/modules/acquisition/simple_growth.py"
  docker cp /opt/vanguard/backend/app/modules/acquisition/simple_limits.py "$c:/app/app/modules/acquisition/simple_limits.py"
  docker exec -u 0 "$c" chown -R appuser:appuser /app/app/modules/acquisition
  docker exec "$c" python -m py_compile /app/app/modules/acquisition/simple_growth.py /app/app/modules/acquisition/simple_limits.py
done
echo "Restarting containers..."
docker restart vanguard-backend vanguard-celery-worker vanguard-celery-beat vanguard-telegram-growth-worker
echo "Done!"
