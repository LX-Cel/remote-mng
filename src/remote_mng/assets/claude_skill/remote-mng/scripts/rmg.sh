#!/usr/bin/env bash
# Portable source fallback. `rmg skill install` replaces this launcher with one
# bound to the Python interpreter of the installed remote-mng distribution.
set -eu
export MSYS2_ARG_CONV_EXCL='*'
export PYTHONIOENCODING=utf-8
exec rmg "$@"
