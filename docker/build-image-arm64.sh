#!/bin/bash

docker buildx build \
  --platform linux/arm64 \
  -t monkey-island-website:v0.1 \
  -f Dockerfile \
  --output type=docker,dest=monkey-island-website-arm64.tar ..
