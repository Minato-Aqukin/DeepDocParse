#!/usr/bin/env bash
set -u

if [ "${1:-}" = "-l" ] || [ "${1:-}" = "--list" ]; then
  if [ "${WSL_SHIM_LIST_EXIT:-0}" != "0" ]; then
    printf '%s\n' "${WSL_SHIM_LIST_STDERR:-shim list failure}" >&2
    exit "${WSL_SHIM_LIST_EXIT}"
  fi
  printf '%s' "${WSL_SHIM_LIST:-}" | iconv -f UTF-8 -t UTF-16LE
  exit 0
fi

if [ "${1:-}" = "--status" ]; then
  printf '%s' "${WSL_SHIM_STATUS:-}" | iconv -f UTF-8 -t UTF-16LE
  exit 0
fi

if [ "${1:-}" = "-d" ]; then
  shift 2
  if [ "${1:-}" = "--" ]; then shift; fi
  "$@"
  exit $?
fi

printf '%s\n' "wsl-shim: unsupported arguments: $*" >&2
exit 64
