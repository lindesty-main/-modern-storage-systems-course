#include "callback_model.h"

#include <errno.h>
#include <stdint.h>
#include <string.h>

#define MODEL_QUEUE_CAPACITY 8
#define MODEL_BLOB_CAPACITY 128
#define MODEL_MAX_EVENTS 32

struct model_context;
typedef void (*model_callback)(struct model_context *ctx, int status);

struct model_event {
    enum model_op op;
    model_callback callback;
};

struct model_queue {
    struct model_event events[MODEL_QUEUE_CAPACITY];
    size_t head;
    size_t count;
};

struct model_context {
    struct model_queue queue;
    struct model_result *result;
    enum model_op fail_at;
    bool initialized;
    bool allocated;
    bool opened;
    bool metadata_synced;
    unsigned char storage[MODEL_BLOB_CAPACITY];
    size_t stored_length;
    unsigned char readback[MODEL_BLOB_CAPACITY];
    size_t readback_length;
};

static const unsigned char payload[] = "ordered callback payload";

const char *model_op_name(enum model_op op)
{
    static const char *const names[] = {
        "none", "init", "create", "open", "resize", "sync_md",
        "write", "read", "close", "unload",
    };

    if ((unsigned)op >= sizeof(names) / sizeof(names[0])) {
        return "invalid";
    }
    return names[op];
}

static int queue_push(struct model_queue *queue, enum model_op op,
                      model_callback callback)
{
    size_t tail;

    if (queue->count == MODEL_QUEUE_CAPACITY) {
        return -EAGAIN;
    }
    tail = (queue->head + queue->count) % MODEL_QUEUE_CAPACITY;
    queue->events[tail].op = op;
    queue->events[tail].callback = callback;
    queue->count++;
    return 0;
}

static bool queue_pop(struct model_queue *queue, struct model_event *event)
{
    if (queue->count == 0) {
        return false;
    }
    *event = queue->events[queue->head];
    queue->head = (queue->head + 1) % MODEL_QUEUE_CAPACITY;
    queue->count--;
    return true;
}

static void finish(struct model_context *ctx, int error)
{
    if (ctx->result->error == 0 && error != 0) {
        ctx->result->error = error;
    }
    ctx->result->done = true;
}

static void record_error(struct model_context *ctx, int error);
static void submit_next(struct model_context *ctx, enum model_op op,
                        model_callback callback);
static void on_init(struct model_context *ctx, int status);
static void on_create(struct model_context *ctx, int status);
static void on_open(struct model_context *ctx, int status);
static void on_resize(struct model_context *ctx, int status);
static void on_sync_md(struct model_context *ctx, int status);
static void on_write(struct model_context *ctx, int status);
static void on_read(struct model_context *ctx, int status);
static void on_close(struct model_context *ctx, int status);
static void on_unload(struct model_context *ctx, int status);

static void start_cleanup(struct model_context *ctx)
{
    if (ctx->opened) {
        submit_next(ctx, MODEL_OP_CLOSE, on_close);
    } else if (ctx->initialized) {
        submit_next(ctx, MODEL_OP_UNLOAD, on_unload);
    } else {
        finish(ctx, ctx->result->error);
    }
}

static void record_error(struct model_context *ctx, int error)
{
    if (ctx->result->error == 0) {
        ctx->result->error = error;
    }
    start_cleanup(ctx);
}

static void submit_next(struct model_context *ctx, enum model_op op,
                        model_callback callback)
{
    int rc = queue_push(&ctx->queue, op, callback);

    if (rc != 0) {
        /* The model submits one continuation at a time; full means broken state. */
        finish(ctx, rc);
    }
}

static int execute_operation(struct model_context *ctx, enum model_op op)
{
    switch (op) {
    case MODEL_OP_INIT:
        ctx->initialized = true;
        return 0;
    case MODEL_OP_CREATE:
        if (!ctx->initialized || ctx->allocated) {
            return -EINVAL;
        }
        ctx->allocated = true;
        return 0;
    case MODEL_OP_OPEN:
        if (!ctx->allocated || ctx->opened) {
            return -EINVAL;
        }
        ctx->opened = true;
        return 0;
    case MODEL_OP_RESIZE:
        if (!ctx->opened || sizeof(payload) - 1 > MODEL_BLOB_CAPACITY) {
            return -EINVAL;
        }
        return 0;
    case MODEL_OP_SYNC_MD:
        if (!ctx->opened) {
            return -EINVAL;
        }
        ctx->metadata_synced = true;
        return 0;
    case MODEL_OP_WRITE:
        if (!ctx->opened || !ctx->metadata_synced ||
            sizeof(payload) - 1 > MODEL_BLOB_CAPACITY) {
            return -EINVAL;
        }
        memcpy(ctx->storage, payload, sizeof(payload) - 1);
        ctx->stored_length = sizeof(payload) - 1;
        return 0;
    case MODEL_OP_READ:
        if (!ctx->opened || ctx->stored_length > sizeof(ctx->readback)) {
            return -EINVAL;
        }
        memcpy(ctx->readback, ctx->storage, ctx->stored_length);
        ctx->readback_length = ctx->stored_length;
        return 0;
    case MODEL_OP_CLOSE:
        if (!ctx->opened) {
            return -EINVAL;
        }
        ctx->opened = false;
        return 0;
    case MODEL_OP_UNLOAD:
        if (!ctx->initialized || ctx->opened) {
            return -EBUSY;
        }
        ctx->allocated = false;
        ctx->metadata_synced = false;
        ctx->initialized = false;
        ctx->stored_length = 0;
        return 0;
    case MODEL_OP_NONE:
        return -EINVAL;
    }
    return -EINVAL;
}

static void on_init(struct model_context *ctx, int status)
{
    if (status != 0) {
        record_error(ctx, status);
        return;
    }
    submit_next(ctx, MODEL_OP_CREATE, on_create);
}

static void on_create(struct model_context *ctx, int status)
{
    if (status != 0) {
        record_error(ctx, status);
        return;
    }
    submit_next(ctx, MODEL_OP_OPEN, on_open);
}

static void on_open(struct model_context *ctx, int status)
{
    if (status != 0) {
        record_error(ctx, status);
        return;
    }
    submit_next(ctx, MODEL_OP_RESIZE, on_resize);
}

static void on_resize(struct model_context *ctx, int status)
{
    if (status != 0) {
        record_error(ctx, status);
        return;
    }
    submit_next(ctx, MODEL_OP_SYNC_MD, on_sync_md);
}

static void on_sync_md(struct model_context *ctx, int status)
{
    if (status != 0) {
        record_error(ctx, status);
        return;
    }
    submit_next(ctx, MODEL_OP_WRITE, on_write);
}

static void on_write(struct model_context *ctx, int status)
{
    if (status != 0) {
        record_error(ctx, status);
        return;
    }
    submit_next(ctx, MODEL_OP_READ, on_read);
}

static void on_read(struct model_context *ctx, int status)
{
    if (status != 0) {
        record_error(ctx, status);
        return;
    }
    if (ctx->readback_length != sizeof(payload) - 1 ||
        memcmp(ctx->readback, payload, sizeof(payload) - 1) != 0) {
        record_error(ctx, -EILSEQ);
        return;
    }
    ctx->result->data_matches = true;
    submit_next(ctx, MODEL_OP_CLOSE, on_close);
}

static void on_close(struct model_context *ctx, int status)
{
    if (status != 0) {
        /* An open handle cannot be safely unloaded; report incomplete cleanup. */
        finish(ctx, status);
        return;
    }
    submit_next(ctx, MODEL_OP_UNLOAD, on_unload);
}

static void on_unload(struct model_context *ctx, int status)
{
    if (status != 0) {
        finish(ctx, status);
        return;
    }
    finish(ctx, 0);
}

static void execute_event(struct model_context *ctx,
                          const struct model_event *event)
{
    int status;
    struct model_result *result = ctx->result;

    if (result->log_count == MODEL_MAX_OPERATIONS) {
        finish(ctx, -EOVERFLOW);
        return;
    }
    result->operations[result->log_count++] = event->op;
    result->events_processed++;

    if (event->op == ctx->fail_at) {
        status = -EIO;
    } else {
        status = execute_operation(ctx, event->op);
    }
    event->callback(ctx, status);
}

int model_run(enum model_op fail_at, struct model_result *result)
{
    struct model_context ctx;
    struct model_event event;
    size_t steps = 0;

    if (result == NULL || (unsigned)fail_at > MODEL_OP_UNLOAD) {
        return -1;
    }
    memset(result, 0, sizeof(*result));
    memset(&ctx, 0, sizeof(ctx));
    ctx.result = result;
    ctx.fail_at = fail_at;

    submit_next(&ctx, MODEL_OP_INIT, on_init);
    while (!result->done && queue_pop(&ctx.queue, &event)) {
        if (++steps > MODEL_MAX_EVENTS) {
            finish(&ctx, -ELOOP);
            break;
        }
        execute_event(&ctx, &event);
    }
    if (!result->done) {
        finish(&ctx, -EIO);
    }

    result->queue_empty = ctx.queue.count == 0;
    result->storage_released = !ctx.initialized && !ctx.allocated && !ctx.opened;
    if (!result->queue_empty && result->error == 0) {
        result->error = -EIO;
    }
    return 0;
}
