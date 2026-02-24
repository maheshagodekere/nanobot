#!/usr/bin/env bash
set -euo pipefail
cd ~/nanobot
docker compose stop "${@:-nanobot-gateway}"
docker compose ps
