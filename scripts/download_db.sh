#!/bin/sh
# Downloads the public Chinook SQLite sample database (MIT licence, Luis Rocha).
# https://github.com/lerocha/chinook-database
set -e
D="$(dirname "$0")/../data"
mkdir -p "$D"
curl -fL -o "$D/Chinook_Sqlite.sqlite" \
  https://github.com/lerocha/chinook-database/releases/download/v1.4.5/Chinook_Sqlite.sqlite
