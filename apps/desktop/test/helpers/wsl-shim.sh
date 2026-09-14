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
  case "${1:-}" in
    --exec|-e)
      # wsl.exe --exec: argv runs directly, no Linux shell in between.
      shift
      "$@"
      exit $?
      ;;
    --)
      # wsl.exe -- <command line>: the rest of the *Windows* command line is handed to
      # the distro's default shell as ONE string. That shell expands $variables and
      # consumes quoting before the requested program runs. The old shim exec'd argv
      # here, so a `-- bash -lc 'root=...; "$root"'` call looked correct in tests while
      # expanding "$root" to empty on a real WSL host.
      shift
      line=$(python3 -c 'import subprocess, sys; sys.stdout.write(subprocess.list2cmdline(sys.argv[1:]))' "$@")
      exec bash -c "$line"
      ;;
  esac
fi

printf '%s\n' "wsl-shim: unsupported arguments: $*" >&2
exit 64
