#!/bin/sh

# Wait for oauth2-proxy to be ready
echo "Waiting for oauth2-proxy to be ready..."
while ! nslookup oauth2-proxy > /dev/null 2>&1; do
    sleep 1
done

echo "Waiting for oauth2-proxy port 4180..."
while ! nc -z oauth2-proxy 4180; do
    sleep 1
done

echo "Waiting for app port 8501..."
while ! nc -z app 8501; do
    sleep 1
done

echo "All services are ready, starting nginx..."
exec nginx -g 'daemon off;'