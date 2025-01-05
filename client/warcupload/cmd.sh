#!/usr/bin/env sh

mkdir -p /input/warcs /input/tmp
sleep 5
while true; do python3 /app/app.py /input/warcs /input/tmp ${TARGET_URL} ${USERNAME}; sleep 120; done
