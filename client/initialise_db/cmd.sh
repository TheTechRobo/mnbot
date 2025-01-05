#!/bin/bash

set -ex

# Ensures all tables are set up. Otherwise, multiple workers will step on each other's toes.
# Also creates some indexes for Chromebot's purposes.

export BROZZLER_RETHINKDB_SERVERS="rethinkdb:28015"

# doublethink should retry until RethinkDB is available, so no need to wait.
brozzler-ensure-tables

