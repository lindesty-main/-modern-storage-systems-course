#!/bin/sh
set -eu

BIN=${1:-./block-image}
TMP=$(mktemp -d "${TMPDIR:-/tmp}/lab-01-block-image.XXXXXX")
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
IMAGE="$TMP/demo.img"

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

expect_failure() {
    description=$1
    shift
    if "$@" >"$TMP/failure.out" 2>&1; then
        fail "$description unexpectedly succeeded"
    fi
}

"$BIN" init "$IMAGE" 512 4 >"$TMP/init.out"
grep -q 'block_size=512' "$TMP/init.out" || fail 'init geometry output'
grep -q 'block_count=4' "$TMP/init.out" || fail 'init block count output'

"$BIN" info "$IMAGE" >"$TMP/info.out"
grep -q 'LBA range=0..3' "$TMP/info.out" || fail 'info zero-based LBA range'

"$BIN" write "$IMAGE" 1 first >"$TMP/write1.out"
grep -q 'byte offset=576' "$TMP/write1.out" || fail 'LBA 1 maps to header 64 + 512'
grep -q 'mapping verified' "$TMP/write1.out" || fail 'write verifies LBA mapping'
"$BIN" read "$IMAGE" 1 >"$TMP/read1.out"
grep -q 'byte offset=576' "$TMP/read1.out" || fail 'read uses same mapping'
grep -q '66 69 72 73 74 00' "$TMP/read1.out" || fail 'payload followed by zero padding'
grep -q 'text preview: first' "$TMP/read1.out" || fail 'read text preview'

full_payload=$(awk 'BEGIN { for (i = 0; i < 512; ++i) printf "x" }')
printf '%s' "$full_payload" >"$TMP/full-nonzero"
"$BIN" write "$IMAGE" 2 "$full_payload" >"$TMP/write-full.out"
dd if="$IMAGE" of="$TMP/full-block.out" bs=1 skip=1088 count=512 2>/dev/null
cmp "$TMP/full-nonzero" "$TMP/full-block.out" || fail '512-byte nonzero block was written before overwrite'
"$BIN" write "$IMAGE" 2 short >"$TMP/write-short.out"
dd if=/dev/zero of="$TMP/expected-zero-tail" bs=1 count=507 2>/dev/null
dd if="$IMAGE" of="$TMP/actual-zero-tail" bs=1 skip=1093 count=507 2>/dev/null
cmp "$TMP/expected-zero-tail" "$TMP/actual-zero-tail" || fail 'short payload overwrite zeroes the entire remaining block'
"$BIN" read "$IMAGE" 2 >"$TMP/read-short.out"
grep -q 'text preview: short' "$TMP/read-short.out" || fail 'short payload replaces previous block contents'

"$BIN" write "$IMAGE" 1 second >"$TMP/write2.out"
"$BIN" read "$IMAGE" 1 >"$TMP/read2.out"
grep -q 'text preview: second' "$TMP/read2.out" || fail 'later write replaces same block'
if grep -q 'first' "$TMP/read2.out"; then
    fail 'old payload remains after whole-block overwrite'
fi

"$BIN" write "$IMAGE" 3 tail >"$TMP/write3.out"
grep -q 'byte offset=1600' "$TMP/write3.out" || fail 'LBA 3 offset calculation'
expect_failure 'out-of-range LBA read' "$BIN" read "$IMAGE" 4
grep -q 'LBA 越界' "$TMP/failure.out" || fail 'range error explains the boundary'
expect_failure 'out-of-range LBA write' "$BIN" write "$IMAGE" 4 must-not-write
grep -q 'LBA 越界' "$TMP/failure.out" || fail 'write range error explains the boundary'
expect_failure 'negative LBA' "$BIN" read "$IMAGE" -1
expect_failure 'non-decimal LBA' "$BIN" read "$IMAGE" 1x

long_payload=$(awk 'BEGIN { for (i = 0; i < 513; ++i) printf "x" }')
expect_failure 'payload larger than one block' "$BIN" write "$IMAGE" 0 "$long_payload"

"$BIN" nlb 512 0 >"$TMP/nlb-512-0.out"
grep -q '^actual_blocks=1$' "$TMP/nlb-512-0.out" || fail 'NLB 0 encodes one logical block'
grep -q '^bytes=512$' "$TMP/nlb-512-0.out" || fail 'NLB 0 at 512 bytes has a 512-byte data length'
"$BIN" nlb 512 7 >"$TMP/nlb-512-7.out"
grep -q '^actual_blocks=8$' "$TMP/nlb-512-7.out" || fail 'NLB 7 encodes eight logical blocks'
grep -q '^bytes=4096$' "$TMP/nlb-512-7.out" || fail 'NLB 7 at 512 bytes has a 4096-byte data length'
"$BIN" nlb 4096 7 >"$TMP/nlb-4096-7.out"
grep -q '^actual_blocks=8$' "$TMP/nlb-4096-7.out" || fail 'NLB 7 encodes eight 4096-byte logical blocks'
grep -q '^bytes=32768$' "$TMP/nlb-4096-7.out" || fail 'NLB 7 at 4096 bytes has a 32768-byte data length'
"$BIN" nlb 512 65535 >"$TMP/nlb-max.out"
grep -q '^actual_blocks=65536$' "$TMP/nlb-max.out" || fail 'maximum 16-bit NLB encodes 65536 blocks'
grep -q '^bytes=33554432$' "$TMP/nlb-max.out" || fail 'maximum NLB at 512 bytes converts without overflow'
expect_failure 'invalid NLB block size' "$BIN" nlb 1024 7
expect_failure 'non-decimal NLB' "$BIN" nlb 512 7x
expect_failure 'negative NLB' "$BIN" nlb 512 -1
expect_failure 'NLB above 16-bit range' "$BIN" nlb 512 65536

expect_failure 'invalid block size' "$BIN" init "$TMP/invalid-size.img" 1024 4
expect_failure 'zero block count' "$BIN" init "$TMP/zero-count.img" 512 0
expect_failure 'count above configured maximum' "$BIN" init "$TMP/too-large.img" 4096 65537
expect_failure 'O_EXCL refuses existing image' "$BIN" init "$IMAGE" 512 4
"$BIN" info "$IMAGE" >"$TMP/info-after-excl.out"
grep -q 'block_count=4' "$TMP/info-after-excl.out" || fail 'O_EXCL leaves existing image intact'

IMAGE_4K="$TMP/4k.img"
"$BIN" init "$IMAGE_4K" 4096 2 >"$TMP/init-4k.out"
"$BIN" write "$IMAGE_4K" 1 four-k >"$TMP/write-4k.out"
grep -q 'byte offset=4160' "$TMP/write-4k.out" || fail '4096-byte LBA geometry mapping'
"$BIN" read "$IMAGE_4K" 1 >"$TMP/read-4k.out"
grep -q 'text preview: four-k' "$TMP/read-4k.out" || fail '4096-byte image round trip'

TRUNCATED_DATA="$TMP/truncated-data.img"
cp "$IMAGE" "$TRUNCATED_DATA"
truncate -s 2111 "$TRUNCATED_DATA"
expect_failure 'valid header with truncated data region' "$BIN" info "$TRUNCATED_DATA"
grep -q '文件长度为' "$TMP/failure.out" || fail 'truncated data is rejected by total file length validation'

BAD_MAGIC="$TMP/bad-magic.img"
cp "$IMAGE" "$BAD_MAGIC"
printf X | dd of="$BAD_MAGIC" bs=1 seek=0 conv=notrunc 2>/dev/null
expect_failure 'corrupted magic' "$BIN" info "$BAD_MAGIC"
grep -q 'magic 不匹配' "$TMP/failure.out" || fail 'corrupt magic is diagnosed'

BAD_CHECKSUM="$TMP/bad-checksum.img"
cp "$IMAGE" "$BAD_CHECKSUM"
printf '\001' | dd of="$BAD_CHECKSUM" bs=1 seek=16 conv=notrunc 2>/dev/null
expect_failure 'corrupted header field/checksum' "$BIN" info "$BAD_CHECKSUM"
grep -q '校验值不匹配' "$TMP/failure.out" || fail 'checksum corruption is diagnosed'

SHORT="$TMP/short.img"
printf 'not-a-block-image' >"$SHORT"
expect_failure 'ordinary file without a valid image header' "$BIN" info "$SHORT"
expect_failure 'directory is not an image file' "$BIN" info "$TMP"
expect_failure 'character device is not an image file' "$BIN" info /dev/null
grep -q '路径必须指向普通文件' "$TMP/failure.out" || fail '/dev/null is rejected by the pre-open file type check'
ln -s "$IMAGE" "$TMP/link.img"
expect_failure 'symlink is not accepted' "$BIN" info "$TMP/link.img"

printf 'PASS: init/info, LBA mapping, read/write, zero padding after nonzero overwrite, NLB conversion, bounds, invalid input, O_EXCL, truncated data, and file/header safety\n'
