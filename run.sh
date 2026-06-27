#!/bin/bash
set -e
cd "$(dirname "$0")"
podman build -t diffedora . && podman run --rm -e ANTHROPIC_API_KEY diffedora "$@"
