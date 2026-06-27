#!/bin/bash
set -e
cd "$(dirname "$0")"

site=$(mktemp -d)
trap "rm -rf '$site'" EXIT

mkdir -p data

build_output=$(podman build -t diffedora . 2>&1) || { echo "$build_output" >&2; exit 1; }

env_flag=$([ -f .env ] && echo "--env-file .env")

podman run --rm $env_flag \
  -v "$PWD/data:/cache:z" \
  diffedora --cache-dir /cache --releases 5 --variant silverblue --output html \
  > "$site/index.html"

podman run --rm $env_flag \
  -v "$PWD/data:/cache:z" \
  diffedora --cache-dir /cache --releases 1 --variant coreos --output html \
  > "$site/coreos.html"

echo "Serving at http://localhost:8080 (Ctrl+C to stop)"
python3 -m http.server 8080 --directory "$site"
