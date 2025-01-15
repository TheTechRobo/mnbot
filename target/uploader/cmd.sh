#!/bin/bash

# Don't change this (if you're using docker-compose)
export ROOT="/input"

while true; do ./to-ia.py; sleep 120; done
