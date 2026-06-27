#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p .cache
build_output=$(podman build -t diffedora . 2>&1) || { echo "$build_output" >&2; exit 1; }
podman run --rm \
  $([ -f .env ] && echo "--env-file .env") \
  -v "$PWD/.cache:/cache:z" \
  diffedora --cache-dir /cache "$@"
