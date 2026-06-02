#!/bin/bash

# Deployment script for Vanguard frontend to xd server
# Usage: ./deploy.sh

echo "Starting deployment to xd server..."

# Configuration
REMOTE_USER="root"
REMOTE_HOST="xd"
REMOTE_PATH="/var/www/vanguard/frontend"
LOCAL_DIST="dist.tar.gz"

# Upload the dist archive
echo "Uploading dist.tar.gz to server..."
scp ${LOCAL_DIST} ${REMOTE_USER}@${REMOTE_HOST}:/tmp/

# Deploy on remote server
echo "Deploying on remote server..."
ssh ${REMOTE_USER}@${REMOTE_HOST} << 'EOF'
  # Backup existing deployment
  if [ -d /var/www/vanguard/frontend ]; then
    echo "Backing up existing deployment..."
    mv /var/www/vanguard/frontend /var/www/vanguard/frontend.backup.$(date +%Y%m%d_%H%M%S)
  fi

  # Create directory
  mkdir -p /var/www/vanguard/frontend

  # Extract new files
  echo "Extracting files..."
  tar -xzf /tmp/dist.tar.gz -C /var/www/vanguard/frontend

  # Set permissions
  echo "Setting permissions..."
  chown -R www-data:www-data /var/www/vanguard/frontend
  chmod -R 755 /var/www/vanguard/frontend

  # Clean up
  rm /tmp/dist.tar.gz

  # Reload nginx
  echo "Reloading nginx..."
  nginx -t && systemctl reload nginx

  echo "Deployment completed successfully!"
EOF

echo "Done!"
