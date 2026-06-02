# ===========================================
# SSL Certificate Setup Script
# ===========================================

#!/bin/bash
set -e

DOMAIN=${1:-vanguard.example.com}
EMAIL=${2:-admin@example.com}
STAGING=${3:-false}

# Staging option for Let's Encrypt
STAGING_FLAG=""
if [ "$STAGING" = "true" ]; then
    STAGING_FLAG="--staging"
fi

echo "Setting up SSL for $DOMAIN..."

# Stop nginx to free port 80 and 443
docker-compose down nginx

# Create directories
mkdir -p data/certbot/www data/certbot/conf/live/$DOMAIN

# Request certificate
docker run --rm \
    -v $(pwd)/data/certbot/www:/var/www/certbot \
    -v $(pwd)/data/certbot/conf:/etc/letsencrypt \
    certbot/certbot \
    certonly \
    --webroot \
    -w /var/www/certbot \
    -d $DOMAIN \
    --email $EMAIL \
    --agree-tos \
    --no-eff-email \
    --keep-until-expiring \
    $STAGING_FLAG

# Copy certificates
cp data/certbot/conf/live/$DOMAIN/fullchain.pem nginx/ssl/
cp data/certbot/conf/live/$DOMAIN/privkey.pem nginx/ssl/

# Restart nginx
docker-compose up -d nginx

echo "SSL setup complete!"
echo "Certificate location: nginx/ssl/"
