#!/bin/bash

# Don't change this (if you're using docker-compose)
export ROOT="/input"

# XXX: CHANGE THESE VALUES
export ITEM_TITLE="mnbot Item Test"
export ITEM_DESC="Someone forgot to fill in the description."
export ITEM_COLLECTION="test_collection"

while true; do ./to-ia.py; sleep 120; done
