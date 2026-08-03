# =============================================================================
# Vanguard Frontend Dockerfile
# Multi-stage build for Vue 3 + Vite application
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder - Build the application
# -----------------------------------------------------------------------------
FROM node:20-alpine as builder

WORKDIR /app
ARG VITE_API_BASE_URL=/api
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

# Copy package files
COPY frontend/package*.json ./

# Install dependencies
RUN npm ci --only=production=false

# Copy source code
COPY frontend/ ./

# Build the application
RUN npm run build

# -----------------------------------------------------------------------------
# Stage 2: Production - Serve with Nginx
# -----------------------------------------------------------------------------
FROM nginx:alpine as production

# Copy built assets
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

# Create nginx log directory
RUN mkdir -p /var/log/nginx

# Expose port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://127.0.0.1/ || exit 1

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
