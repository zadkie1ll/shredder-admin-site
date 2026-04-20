#!/bin/bash

docker buildx build \
  --platform linux/amd64 \
  -t monkey-island-website:v0.1 \
  -f Dockerfile \
  --output type=docker,dest=monkey-island-website-amd64.tar ..
