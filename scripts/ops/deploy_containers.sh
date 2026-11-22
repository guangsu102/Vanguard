#!/bin/bash
set -e

CONTAINERS=(
  "vanguard-backend"
  "vanguard-celery-worker"
  "vanguard-celery-beat"
  "vanguard-telegram-growth-worker"
)

SRC_DIR="/opt/vanguard/backend/app/modules/acquisition"
FILES=(
  "member_permissions.py"
  "simple_growth.py"
  "simple_limits.py"
)

echo "=== Copying files to containers ==="
for c in "${CONTAINERS[@]}"; do
  echo "Target container: $c"
  for f in "${FILES[@]}"; do
    docker cp "$SRC_DIR/$f" "$c:/app/app/modules/acquisition/$f"
  done
  docker exec "$c" python -m py_compile \
    /app/app/modules/acquisition/member_permissions.py \
    /app/app/modules/acquisition/simple_growth.py \
    /app/app/modules/acquisition/simple_limits.py
done

echo "=== Verifying is_banned in container member_permissions.py ==="
for c in "${CONTAINERS[@]}"; do
  echo -n "$c: "
  docker exec "$c" grep -c "is_banned" /app/app/modules/acquisition/member_permissions.py
done

echo "=== Restarting worker containers ==="
docker restart vanguard-telegram-growth-worker vanguard-celery-worker vanguard-celery-beat vanguard-backend

echo "=== Done ==="
