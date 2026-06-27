#!/bin/bash
set -e
cd "$(dirname "$0")"
[ -f .env ] && source .env
mkdir -p ~/.cache/diffedora
podman build -t diffedora . && podman run --rm \
  -e ANTHROPIC_API_KEY \
  -v ~/.cache/diffedora:/cache:z \
  diffedora --cache-dir /cache "$@"
