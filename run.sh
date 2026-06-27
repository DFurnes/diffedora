#!/bin/bash
set -e
cd "$(dirname "$0")"
mkdir -p data
build_output=$(podman build -t diffedora . 2>&1) || { echo "$build_output" >&2; exit 1; }
podman run --rm \
  $([ -f .env ] && echo "--env-file .env") \
  -v "$PWD/data:/cache:z" \
  diffedora --cache-dir /cache "$@"
