#!/usr/bin/env bash
set -euo pipefail
cd ~/nanobot
docker compose restart "${@:-nanobot-gateway}"
docker compose ps
