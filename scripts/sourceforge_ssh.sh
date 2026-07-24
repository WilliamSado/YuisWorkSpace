#!/usr/bin/env bash
set -euo pipefail

# The server cannot reach SourceForge SSH directly, so tunnel SSH through the
# local mixed HTTP proxy managed by iKuuuVPN.
exec ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=30 \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=6 \
  -o 'ProxyCommand=nc -X connect -x 127.0.0.1:7890 %h %p' \
  "$@"
