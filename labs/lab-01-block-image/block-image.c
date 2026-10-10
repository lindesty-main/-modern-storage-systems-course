#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define HEADER_SIZE 64U
#define MIN_BLOCK_COUNT 1U
#define MAX_BLOCK_COUNT 65536U
#define MAX_BLOCK_SIZE 4096U
#define MIN_BLOCK_SIZE 512U
#define PREVIEW_BYTES 64U

static const unsigned char image_magic[8] = {'B', 'L', 'K', 'I', 'M', 'G', '0', '1'};

struct image_info {
    int fd;
    uint32_t block_size;
    uint64_t block_count;
    uint64_t data_bytes;
};

static void put_u32le(unsigned char *p, uint32_t value)
{
    p[0] = (unsigned char)(value & 0xffU);
    p[1] = (unsigned char)((value >> 8) & 0xffU);
    p[2] = (unsigned char)((value >> 16) & 0xffU);
    p[3] = (unsigned char)((value >> 24) & 0xffU);
}

static void put_u64le(unsigned char *p, uint64_t value)
{
    for (unsigned int i = 0; i < 8U; ++i) {
        p[i] = (unsigned char)((value >> (8U * i)) & 0xffU);
    }
}

static uint32_t get_u32le(const unsigned char *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static uint64_t get_u64le(const unsigned char *p)
{
    uint64_t value = 0;
    for (unsigned int i = 0; i < 8U; ++i) {
        value |= (uint64_t)p[i] << (8U * i);
    }
    return value;
}

static uint32_t header_checksum(const unsigned char header[HEADER_SIZE])
{
    uint32_t hash = UINT32_C(2166136261);
    for (unsigned int i = 0; i < HEADER_SIZE; ++i) {
        if (i >= 40U && i < 44U) {
            continue;
        }
        hash ^= header[i];
        hash *= UINT32_C(16777619);
    }
    return hash;
}

static int pread_full(int fd, void *buffer, size_t length, off_t offset)
{
    size_t done = 0;
    while (done < length) {
        ssize_t result = pread(fd, (unsigned char *)buffer + done, length - done,
                               offset + (off_t)done);
        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (result == 0) {
            errno = EIO;
            return -1;
        }
        done += (size_t)result;
    }
    return 0;
}

static int pwrite_full(int fd, const void *buffer, size_t length, off_t offset)
{
    size_t done = 0;
    while (done < length) {
        ssize_t result = pwrite(fd, (const unsigned char *)buffer + done,
                                length - done, offset + (off_t)done);
        if (result < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (result == 0) {
            errno = EIO;
            return -1;
        }
        done += (size_t)result;
    }
    return 0;
}

static bool valid_geometry(uint32_t block_size, uint64_t block_count,
                           uint64_t *data_bytes, uint64_t *file_bytes)
{
    if ((block_size != 512U && block_size != 4096U) ||
        block_count < MIN_BLOCK_COUNT || block_count > MAX_BLOCK_COUNT) {
        return false;
    }
    if (block_count > UINT64_MAX / block_size) {
        return false;
    }
    *data_bytes = block_count * block_size;
    if (*data_bytes > UINT64_MAX - HEADER_SIZE) {
        return false;
    }
    *file_bytes = HEADER_SIZE + *data_bytes;
    if (*file_bytes > (uint64_t)INT64_MAX) {
        return false;
    }
    return true;
}

static int parse_u64(const char *text, uint64_t *value)
{
    if (text == NULL || text[0] == '\0') {
        return -1;
    }
    for (const unsigned char *p = (const unsigned char *)text; *p != '\0'; ++p) {
        if (!isdigit(*p)) {
            return -1;
        }
    }
    errno = 0;
    char *end = NULL;
    unsigned long long parsed = strtoull(text, &end, 10);
    if (errno == ERANGE || end == text || *end != '\0') {
        return -1;
    }
    *value = (uint64_t)parsed;
    return 0;
}

static void encode_header(unsigned char header[HEADER_SIZE], uint32_t block_size,
                          uint64_t block_count, uint64_t data_bytes)
{
    memset(header, 0, HEADER_SIZE);
    memcpy(header, image_magic, sizeof(image_magic));
    put_u32le(header + 8U, 1U);             /* format version */
    put_u32le(header + 12U, HEADER_SIZE);  /* header length */
    put_u32le(header + 16U, block_size);
    put_u32le(header + 20U, 0U);           /* flags reserved for version 1 */
    put_u64le(header + 24U, block_count);
    put_u64le(header + 32U, data_bytes);
    put_u32le(header + 40U, header_checksum(header));
}

static int validate_header(const unsigned char header[HEADER_SIZE], uint64_t file_size,
                           struct image_info *info)
{
    if (memcmp(header, image_magic, sizeof(image_magic)) != 0) {
        fprintf(stderr, "拒绝映像：magic 不匹配（不是本实验创建的块映像）\n");
        return -1;
    }
    if (get_u32le(header + 8U) != 1U || get_u32le(header + 12U) != HEADER_SIZE ||
        get_u32le(header + 20U) != 0U) {
        fprintf(stderr, "拒绝映像：格式版本、header 长度或保留字段无效\n");
        return -1;
    }
    for (unsigned int i = 44U; i < HEADER_SIZE; ++i) {
        if (header[i] != 0U) {
            fprintf(stderr, "拒绝映像：保留字节非零\n");
            return -1;
        }
    }
    if (get_u32le(header + 40U) != header_checksum(header)) {
        fprintf(stderr, "拒绝映像：header 校验值不匹配\n");
        return -1;
    }

    uint32_t block_size = get_u32le(header + 16U);
    uint64_t block_count = get_u64le(header + 24U);
    uint64_t data_bytes = 0;
    uint64_t expected_file_bytes = 0;
    if (!valid_geometry(block_size, block_count, &data_bytes, &expected_file_bytes) ||
        get_u64le(header + 32U) != data_bytes) {
        fprintf(stderr, "拒绝映像：几何参数非法或数据区长度不一致\n");
        return -1;
    }
    if (file_size != expected_file_bytes) {
        fprintf(stderr, "拒绝映像：文件长度为 %" PRIu64
                        "，header 声明长度为 %" PRIu64 "\n",
                file_size, expected_file_bytes);
        return -1;
    }
    info->block_size = block_size;
    info->block_count = block_count;
    info->data_bytes = data_bytes;
    return 0;
}

static int open_image(const char *path, bool writable, struct image_info *info)
{
    struct stat before;
    if (lstat(path, &before) != 0) {
        perror("lstat image");
        return -1;
    }
    if (!S_ISREG(before.st_mode)) {
        fprintf(stderr, "拒绝映像：路径必须指向普通文件（不接受块设备、目录、FIFO 或符号链接）\n");
        return -1;
    }

    int flags = (writable ? O_RDWR : O_RDONLY) | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK;
    int fd = open(path, flags);
    if (fd < 0) {
        perror("open image");
        return -1;
    }

    struct stat after;
    if (fstat(fd, &after) != 0) {
        perror("fstat image");
        (void)close(fd);
        return -1;
    }
    if (!S_ISREG(after.st_mode) || before.st_dev != after.st_dev || before.st_ino != after.st_ino) {
        fprintf(stderr, "拒绝映像：打开时文件类型或 inode 发生变化\n");
        (void)close(fd);
        return -1;
    }
    if (after.st_size < (off_t)HEADER_SIZE) {
        fprintf(stderr, "拒绝映像：文件短于固定 header\n");
        (void)close(fd);
        return -1;
    }

    unsigned char header[HEADER_SIZE];
    if (pread_full(fd, header, sizeof(header), 0) != 0) {
        perror("read image header");
        (void)close(fd);
        return -1;
    }
    if (validate_header(header, (uint64_t)after.st_size, info) != 0) {
        (void)close(fd);
        return -1;
    }
    info->fd = fd;
    return 0;
}

static int calculate_offset(const struct image_info *info, uint64_t lba, off_t *offset)
{
    if (lba >= info->block_count) {
        fprintf(stderr, "LBA 越界：LBA=%" PRIu64 "，合法范围为 0..%" PRIu64 "\n",
                lba, info->block_count - 1U);
        return -1;
    }
    if (lba > (UINT64_MAX - HEADER_SIZE) / info->block_size) {
        fprintf(stderr, "LBA 到字节偏移换算溢出\n");
        return -1;
    }
    uint64_t byte_offset = HEADER_SIZE + lba * info->block_size;
    uint64_t data_offset = byte_offset - HEADER_SIZE;
    if (data_offset % info->block_size != 0U ||
        data_offset / info->block_size != lba ||
        byte_offset + info->block_size > HEADER_SIZE + info->data_bytes ||
        byte_offset > (uint64_t)INT64_MAX) {
        fprintf(stderr, "内部映射校验失败\n");
        return -1;
    }
    *offset = (off_t)byte_offset;
    return 0;
}

static int command_init(const char *path, const char *block_size_arg,
                        const char *block_count_arg)
{
    uint64_t parsed_block_size = 0;
    uint64_t block_count = 0;
    if (parse_u64(block_size_arg, &parsed_block_size) != 0 ||
        parsed_block_size > UINT32_MAX || parse_u64(block_count_arg, &block_count) != 0) {
        fprintf(stderr, "非法几何参数：块大小仅可为 512 或 4096，块数为十进制正整数\n");
        return 1;
    }
    uint32_t block_size = (uint32_t)parsed_block_size;
    uint64_t data_bytes = 0;
    uint64_t file_bytes = 0;
    if (!valid_geometry(block_size, block_count, &data_bytes, &file_bytes)) {
        fprintf(stderr, "几何参数超出限制：block-size=512/4096，block-count=1..%u\n",
                MAX_BLOCK_COUNT);
        return 1;
    }

    int fd = open(path, O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        perror("create image (O_EXCL; existing paths are never overwritten)");
        return 1;
    }
    struct stat created_stat;
    if (fstat(fd, &created_stat) != 0) {
        int saved_errno = errno;
        perror("fstat new image");
        (void)close(fd);
        (void)unlink(path); /* O_EXCL created this name; remove the incomplete image. */
        errno = saved_errno;
        return 1;
    }
    if (!S_ISREG(created_stat.st_mode)) {
        errno = EINVAL;
        perror("verify new image");
        (void)close(fd);
        struct stat path_stat;
        if (lstat(path, &path_stat) == 0 && S_ISREG(path_stat.st_mode) &&
            path_stat.st_dev == created_stat.st_dev && path_stat.st_ino == created_stat.st_ino) {
            (void)unlink(path);
        }
        return 1;
    }

    unsigned char header[HEADER_SIZE];
    encode_header(header, block_size, block_count, data_bytes);
    if (ftruncate(fd, (off_t)file_bytes) != 0 ||
        pwrite_full(fd, header, sizeof(header), 0) != 0 || fdatasync(fd) != 0) {
        int saved_errno = errno;
        perror("initialize image");
        (void)close(fd);
        struct stat path_stat;
        if (lstat(path, &path_stat) == 0 && S_ISREG(path_stat.st_mode) &&
            path_stat.st_dev == created_stat.st_dev && path_stat.st_ino == created_stat.st_ino) {
            (void)unlink(path);
        }
        errno = saved_errno;
        return 1;
    }
    if (close(fd) != 0) {
        int saved_errno = errno;
        perror("close image");
        struct stat path_stat;
        if (lstat(path, &path_stat) == 0 && S_ISREG(path_stat.st_mode) &&
            path_stat.st_dev == created_stat.st_dev && path_stat.st_ino == created_stat.st_ino) {
            (void)unlink(path);
        }
        errno = saved_errno;
        return 1;
    }

    printf("created image: %s\nblock_size=%" PRIu32 " bytes\nblock_count=%" PRIu64
           "\ndata_bytes=%" PRIu64 "\nfile_bytes=%" PRIu64 " (includes %u-byte header)\n",
           path, block_size, block_count, data_bytes, file_bytes, HEADER_SIZE);
    return 0;
}

static int command_nlb(const char *block_size_arg, const char *nlb_arg)
{
    uint64_t block_size = 0;
    uint64_t nlb = 0;
    if (parse_u64(block_size_arg, &block_size) != 0 ||
        (block_size != 512U && block_size != 4096U)) {
        fprintf(stderr, "非法块大小：仅可为 512 或 4096 字节\n");
        return 1;
    }
    if (parse_u64(nlb_arg, &nlb) != 0) {
        fprintf(stderr, "非法 NLB：请输入 0..65535 的十进制整数\n");
        return 1;
    }
    if (nlb > UINT16_MAX) {
        fprintf(stderr, "NLB 越界：合法字段范围为 0..%u\n", UINT16_MAX);
        return 1;
    }

    uint64_t actual_blocks = nlb + 1U;
    uint64_t bytes = actual_blocks * block_size;
    printf("field_conversion=NVMe NVM Read/Write NLB (zero-based); no device I/O\n"
           "block_size=%" PRIu64 " bytes\nNLB=%" PRIu64
           "\nactual_blocks=%" PRIu64 "\nbytes=%" PRIu64 "\n",
           block_size, nlb, actual_blocks, bytes);
    return 0;
}

static int command_info(const char *path)
{
    struct image_info info = {.fd = -1};
    if (open_image(path, false, &info) != 0) {
        return 1;
    }
    printf("image=%s\nformat=BLKIMG01/v1\nblock_size=%" PRIu32
           " bytes\nblock_count=%" PRIu64 "\ndata_bytes=%" PRIu64
           "\nLBA range=0..%" PRIu64 " (zero-based)\n",
           path, info.block_size, info.block_count, info.data_bytes, info.block_count - 1U);
    (void)close(info.fd);
    return 0;
}

static int command_write(const char *path, const char *lba_arg, const char *payload)
{
    uint64_t lba = 0;
    if (parse_u64(lba_arg, &lba) != 0) {
        fprintf(stderr, "非法 LBA：请输入非负十进制整数\n");
        return 1;
    }
    struct image_info info = {.fd = -1};
    if (open_image(path, true, &info) != 0) {
        return 1;
    }
    size_t payload_length = strlen(payload);
    if (payload_length > info.block_size) {
        fprintf(stderr, "payload 太长：%zu 字节，大于单块上限 %" PRIu32 "\n",
                payload_length, info.block_size);
        (void)close(info.fd);
        return 1;
    }
    off_t offset = 0;
    if (calculate_offset(&info, lba, &offset) != 0) {
        (void)close(info.fd);
        return 1;
    }

    unsigned char block[MAX_BLOCK_SIZE] = {0};
    memcpy(block, payload, payload_length);
    if (pwrite_full(info.fd, block, info.block_size, offset) != 0) {
        perror("write block");
        (void)close(info.fd);
        return 1;
    }
    if (fdatasync(info.fd) != 0) {
        perror("fdatasync after block write");
        (void)close(info.fd);
        return 1;
    }
    printf("write complete: LBA=%" PRIu64 " -> image byte offset=%" PRId64
           " (header %u + LBA * block_size); mapping verified; payload=%zu bytes, "
           "block=%" PRIu32 " bytes, zero-padded; later writes replace this whole block\n",
           lba, (int64_t)offset, HEADER_SIZE, payload_length, info.block_size);
    (void)close(info.fd);
    return 0;
}

static void print_escaped_text(const unsigned char *data, size_t length)
{
    printf("text preview: ");
    size_t shown = 0;
    while (shown < length && data[shown] != 0U) {
        unsigned char ch = data[shown];
        if (ch >= 32U && ch <= 126U && ch != '\\') {
            putchar((int)ch);
        } else if (ch == '\\') {
            fputs("\\\\", stdout);
        } else {
            printf("\\x%02x", (unsigned int)ch);
        }
        ++shown;
    }
    if (shown == 0U) {
        fputs("<empty>", stdout);
    }
    putchar('\n');
}

static int command_read(const char *path, const char *lba_arg)
{
    uint64_t lba = 0;
    if (parse_u64(lba_arg, &lba) != 0) {
        fprintf(stderr, "非法 LBA：请输入非负十进制整数\n");
        return 1;
    }
    struct image_info info = {.fd = -1};
    if (open_image(path, false, &info) != 0) {
        return 1;
    }
    off_t offset = 0;
    if (calculate_offset(&info, lba, &offset) != 0) {
        (void)close(info.fd);
        return 1;
    }
    unsigned char block[MAX_BLOCK_SIZE] = {0};
    if (pread_full(info.fd, block, info.block_size, offset) != 0) {
        perror("read block");
        (void)close(info.fd);
        return 1;
    }
    size_t preview = info.block_size < PREVIEW_BYTES ? info.block_size : PREVIEW_BYTES;
    printf("read complete: LBA=%" PRIu64 " -> image byte offset=%" PRId64
           "; mapping verified\nhex preview (%zu/%" PRIu32 " bytes):",
           lba, (int64_t)offset, preview, info.block_size);
    for (size_t i = 0; i < preview; ++i) {
        printf(" %02x", (unsigned int)block[i]);
    }
    putchar('\n');
    print_escaped_text(block, preview);
    (void)close(info.fd);
    return 0;
}

static void usage(const char *program)
{
    fprintf(stderr,
            "用法：\n"
            "  %s init IMAGE BLOCK_SIZE BLOCK_COUNT\n"
            "  %s info IMAGE\n"
            "  %s write IMAGE LBA PAYLOAD\n"
            "  %s read IMAGE LBA\n"
            "  %s nlb BLOCK_SIZE NLB\n"
            "限制：BLOCK_SIZE=512 或 4096；BLOCK_COUNT=1..%u；只接受本程序创建且 header 有效的普通文件。\n"
            "  nlb 仅演示 NVMe NVM Read/Write 字段换算，不提交设备请求。\n",
            program, program, program, program, program, MAX_BLOCK_COUNT);
}

int main(int argc, char **argv)
{
    if (argc >= 2 && strcmp(argv[1], "init") == 0 && argc == 5) {
        return command_init(argv[2], argv[3], argv[4]);
    }
    if (argc == 3 && strcmp(argv[1], "info") == 0) {
        return command_info(argv[2]);
    }
    if (argc == 5 && strcmp(argv[1], "write") == 0) {
        return command_write(argv[2], argv[3], argv[4]);
    }
    if (argc == 4 && strcmp(argv[1], "read") == 0) {
        return command_read(argv[2], argv[3]);
    }
    if (argc == 4 && strcmp(argv[1], "nlb") == 0) {
        return command_nlb(argv[2], argv[3]);
    }
    usage(argv[0]);
    return 2;
}
