#!/usr/bin/env bash
set -euo pipefail

cd ~/nanobot

echo "==> Pulling latest from deploy..."
git pull origin deploy

echo "==> Building and starting services..."
docker compose up -d nanobot-gateway nanobot-scraper --build

echo "==> Done. Checking status..."
docker compose ps
