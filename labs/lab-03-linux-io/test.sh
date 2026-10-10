#!/bin/sh
set -eu

output=$(./io-lab)
printf '%s\n' "$output"
for expected in \
    'PASS: pread/pwrite offsets and full-transfer handling' \
    'PASS: writev/readv segment ordering and partial-vector handling' \
    'PASS: local socketpair vectored round-trip' \
    'PASS: invalid vector and offset bounds rejected' \
    'PASS: private temporary files removed'; do
    printf '%s\n' "$output" | grep -F "$expected" >/dev/null || {
        printf 'missing expected result: %s\n' "$expected" >&2
        exit 1
    }
done
case "$output" in
    *'PASS: O_DIRECT aligned temporary-file read'*) ;;
    *'SKIP: O_DIRECT'*) ;;
    *) echo 'missing O_DIRECT result (PASS or explicit SKIP)' >&2; exit 1 ;;
esac
