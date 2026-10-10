#!/bin/sh
set -eu

set +e
output=$(./uring-file 2>&1)
status=$?
set -e
printf '%s\n' "$output"
case "$status" in
    0)
        printf '%s\n' "$output" | grep -F 'PASS: io_uring temporary-file write/read completions verified' >/dev/null
        ;;
    77)
        printf '%s\n' "$output" | grep -F 'SKIP: io_uring unavailable or restricted' >/dev/null
        ;;
    *)
        echo "uring-file failed with status $status" >&2
        exit "$status"
        ;;
esac
