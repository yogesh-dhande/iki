#!/bin/sh
# Generate migrations for a new plugin or a change that needs no rename decision.
set -eu

iki plugin validate "$@"
iki plugin makemigrations "$@" --no-input
