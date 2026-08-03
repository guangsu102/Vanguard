# ===========================================
# Deployment Script
# ===========================================

#!/bin/bash
set -e

ENV=${1:-production}
VERSION=${2:-latest}

echo "Deploying Vanguard to $ENV (version: $VERSION)"

# Load environment variables
if [ -f .env.$ENV ]; then
    export $(grep -v '^#' .env.$ENV | xargs)
else
    echo "Error: .env.$ENV not found"
    exit 1
fi

# Pull latest images
echo "Pulling images..."
docker-compose pull

# Backup database (production only)
if [ "$ENV" = "production" ]; then
    echo "Backing up database..."
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    docker-compose exec -T postgres pg_dump -U ${POSTGRES_USER:-vanguard} > backups/db_backup_$TIMESTAMP.sql
    echo "Backup saved to backups/db_backup_$TIMESTAMP.sql"
fi

# Run migrations
echo "Running database migrations..."
docker-compose run --rm backend sh -c 'PYTHONPATH=/app python /app/scripts/apply_sql_migrations.py'

# Restart services
echo "Restarting services..."
docker-compose up -d --no-deps

# Wait for health check
echo "Waiting for services to be healthy..."
sleep 10

# Verify deployment
if curl -sf http://localhost:8000/health > /dev/null; then
    echo "Deployment successful!"
else
    echo "Health check failed!"
    docker-compose logs
    exit 1
fi

# Show running containers
echo "Running containers:"
docker-compose ps
