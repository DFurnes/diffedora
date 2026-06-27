#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p .cache
podman build -t diffedora . && podman run --rm \
  $([ -f .env ] && echo "--env-file .env") \
  -v "$PWD/.cache:/cache:z" \
  diffedora --cache-dir /cache "$@"
