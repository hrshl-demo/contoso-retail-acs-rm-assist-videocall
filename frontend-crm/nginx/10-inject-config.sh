#!/bin/sh
# Injects TOOLAPI_URL, TOOLAPI_BEARER and VIDEOASSIST_URL into index.html at startup.
set -e
: "${TOOLAPI_URL:=https://invalid.local}"
: "${TOOLAPI_BEARER:=missing-bearer}"
: "${VIDEOASSIST_URL:=https://invalid.local}"   # Step 7 video-call app (phase9 output)
INDEX="/usr/share/nginx/html/index.html"
sed -i "s|__TOOLAPI_URL__|${TOOLAPI_URL}|g" "$INDEX"
sed -i "s|__TOOLAPI_BEARER__|${TOOLAPI_BEARER}|g" "$INDEX"
sed -i "s|__VIDEOASSIST_URL__|${VIDEOASSIST_URL}|g" "$INDEX"
echo "[entrypoint] injected TOOLAPI_URL=${TOOLAPI_URL} VIDEOASSIST_URL=${VIDEOASSIST_URL} (bearer length ${#TOOLAPI_BEARER})"
