#!/bin/sh
# Run the checks shared by first-party and standalone plugins.
set -eu

iki plugin validate "$@"
iki plugin makemigrations "$@" --check --no-input
iki plugin test "$@"
