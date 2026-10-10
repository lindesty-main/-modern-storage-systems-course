#ifndef CALLBACK_MODEL_H
#define CALLBACK_MODEL_H

#include <stdbool.h>
#include <stddef.h>

enum model_op {
    MODEL_OP_NONE = 0,
    MODEL_OP_INIT,
    MODEL_OP_CREATE,
    MODEL_OP_OPEN,
    MODEL_OP_RESIZE,
    MODEL_OP_SYNC_MD,
    MODEL_OP_WRITE,
    MODEL_OP_READ,
    MODEL_OP_CLOSE,
    MODEL_OP_UNLOAD,
};

#define MODEL_MAX_OPERATIONS 16

struct model_result {
    int error;
    bool done;
    bool data_matches;
    bool queue_empty;
    bool storage_released;
    size_t events_processed;
    size_t log_count;
    enum model_op operations[MODEL_MAX_OPERATIONS];
};

/*
 * Run a deterministic, in-memory callback chain. fail_at injects -EIO at
 * one operation; no SPDK headers, devices, files, or privileges are used.
 * The function returns -1 only for invalid arguments/model setup failure;
 * operation results are reported in result->error (zero means success).
 */
int model_run(enum model_op fail_at, struct model_result *result);

const char *model_op_name(enum model_op op);

#endif
