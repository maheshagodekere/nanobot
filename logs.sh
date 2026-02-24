#!/usr/bin/env bash
set -euo pipefail
cd ~/nanobot
docker compose logs -f --tail 50 "${@:-nanobot-gateway}"
