#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p logs/assistant-swift-cache
xcrun swiftc -module-cache-path logs/assistant-swift-cache scripts/assistant-vision.swift -o scripts/assistant-vision-ocr
