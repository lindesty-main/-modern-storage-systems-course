#!/bin/sh
set -eu

if [ "$(id -u)" -eq 0 ]; then
    echo 'SKIP: optional FUSE mount test must run as an unprivileged user'
    exit 0
fi
if ! command -v pkg-config >/dev/null 2>&1 || ! pkg-config --exists fuse3; then
    echo 'SKIP: libfuse3 development files are unavailable'
    exit 0
fi
if ! command -v fusermount3 >/dev/null 2>&1 ||
   ! command -v mountpoint >/dev/null 2>&1; then
    echo 'SKIP: fusermount3 and mountpoint utilities are required'
    exit 0
fi
if [ ! -r /dev/fuse ] || [ ! -w /dev/fuse ]; then
    echo 'SKIP: /dev/fuse is not accessible to this user'
    exit 0
fi

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/m03-fuse.XXXXXX")
mount_dir="$work_dir/mount"
log_file="$work_dir/fuse.log"
mkdir "$mount_dir"
fuse_pid=

cleanup()
{
    status=$?
    trap - EXIT HUP INT TERM
    if mountpoint -q "$mount_dir" 2>/dev/null; then
        fusermount3 -u "$mount_dir" >/dev/null 2>&1 || true
    fi
    if [ -n "$fuse_pid" ]; then
        kill -TERM "$fuse_pid" >/dev/null 2>&1 || true
        wait "$fuse_pid" >/dev/null 2>&1 || true
        fuse_pid=
    fi
    if mountpoint -q "$mount_dir" 2>/dev/null; then
        fusermount3 -u "$mount_dir" >/dev/null 2>&1 || true
    fi
    if mountpoint -q "$mount_dir" 2>/dev/null; then
        echo "ERROR: could not unmount temporary mount $mount_dir; inspect and unmount it manually" >&2
        status=1
    else
        rm -rf -- "$work_dir"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

start_mount()
{
    mode=$1
    ./fuse-memfs "$mode" -f -s "$mount_dir" >"$log_file" 2>&1 &
    fuse_pid=$!
    attempt=0
    while ! mountpoint -q "$mount_dir"; do
        if ! kill -0 "$fuse_pid" 2>/dev/null; then
            wait "$fuse_pid" || true
            fuse_pid=
            if grep -Eiq 'operation not permitted|permission denied|failed to open /dev/fuse|fusermount3.*(failed|denied)' "$log_file"; then
                echo 'SKIP: kernel or sandbox policy denied the unprivileged FUSE mount'
                return 77
            fi
            cat "$log_file" >&2
            echo 'ERROR: FUSE process exited before mounting' >&2
            return 1
        fi
        attempt=$((attempt + 1))
        if [ "$attempt" -ge 50 ]; then
            cat "$log_file" >&2
            echo 'ERROR: timed out waiting for the temporary FUSE mount' >&2
            return 1
        fi
        sleep 0.1
    done
}

stop_mount()
{
    fusermount3 -u "$mount_dir"
    wait "$fuse_pid" || true
    fuse_pid=
    if mountpoint -q "$mount_dir"; then
        echo 'ERROR: temporary FUSE mount remained after unmount' >&2
        return 1
    fi
}

if start_mount --memory; then
    :
else
    result=$?
    [ "$result" -eq 77 ] && exit 0
    exit "$result"
fi
listing=$(ls -1 "$mount_dir")
printf '%s\n' "$listing" | grep -Fx 'hello.txt' >/dev/null
printf 'hello' > "$mount_dir/hello.txt"
printf ' world' >> "$mount_dir/hello.txt"
[ "$(cat "$mount_dir/hello.txt")" = 'hello world' ]
[ "$(wc -c < "$mount_dir/hello.txt" | tr -d '[:space:]')" = 11 ]
printf 'replaced' > "$mount_dir/hello.txt"
[ "$(cat "$mount_dir/hello.txt")" = 'replaced' ]
stop_mount
echo 'PASS: mounted memory FUSE lookup/readdir/open/read/write/append'

if start_mount --readonly; then
    :
else
    result=$?
    [ "$result" -eq 77 ] && exit 0
    exit "$result"
fi
[ "$(cat "$mount_dir/hello.txt")" = 'Hello from read-only memfs!' ]
if (printf x > "$mount_dir/hello.txt") 2>/dev/null; then
    echo 'ERROR: read-only FUSE mount accepted a write' >&2
    exit 1
fi
stop_mount
echo 'PASS: mounted read-only FUSE read and write rejection'
