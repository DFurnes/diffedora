#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p ~/.cache/diffedora
podman build -t diffedora . && podman run --rm \
  $([ -f .env ] && echo "--env-file .env") \
  -v ~/.cache/diffedora:/cache:z \
  diffedora --cache-dir /cache "$@"
