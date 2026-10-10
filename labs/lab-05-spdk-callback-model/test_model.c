#include "callback_model.h"

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdio.h>

static void expect_operations(const struct model_result *result,
                             const enum model_op *expected, size_t count)
{
    assert(result->log_count == count);
    for (size_t i = 0; i < count; ++i) {
        assert(result->operations[i] == expected[i]);
    }
}

static void test_successful_callback_chain(void)
{
    static const enum model_op expected[] = {
        MODEL_OP_INIT, MODEL_OP_CREATE, MODEL_OP_OPEN, MODEL_OP_RESIZE,
        MODEL_OP_SYNC_MD, MODEL_OP_WRITE, MODEL_OP_READ, MODEL_OP_CLOSE,
        MODEL_OP_UNLOAD,
    };
    struct model_result result;

    assert(model_run(MODEL_OP_NONE, &result) == 0);
    assert(result.error == 0);
    assert(result.done);
    assert(result.data_matches);
    assert(result.queue_empty);
    assert(result.events_processed == sizeof(expected) / sizeof(expected[0]));
    assert(result.storage_released);
    expect_operations(&result, expected, sizeof(expected) / sizeof(expected[0]));
}

static void test_io_failure_still_cleans_up(void)
{
    static const enum model_op expected[] = {
        MODEL_OP_INIT, MODEL_OP_CREATE, MODEL_OP_OPEN, MODEL_OP_RESIZE,
        MODEL_OP_SYNC_MD, MODEL_OP_WRITE, MODEL_OP_CLOSE, MODEL_OP_UNLOAD,
    };
    struct model_result result;

    assert(model_run(MODEL_OP_WRITE, &result) == 0);
    assert(result.error == -EIO);
    assert(result.done);
    assert(!result.data_matches);
    assert(result.queue_empty);
    assert(result.storage_released);
    expect_operations(&result, expected, sizeof(expected) / sizeof(expected[0]));
}

static void test_close_failure_is_not_misreported_as_clean(void)
{
    static const enum model_op expected[] = {
        MODEL_OP_INIT, MODEL_OP_CREATE, MODEL_OP_OPEN, MODEL_OP_RESIZE,
        MODEL_OP_SYNC_MD, MODEL_OP_WRITE, MODEL_OP_READ, MODEL_OP_CLOSE,
    };
    struct model_result result;

    assert(model_run(MODEL_OP_CLOSE, &result) == 0);
    assert(result.error == -EIO);
    assert(result.done);
    assert(result.queue_empty);
    assert(!result.storage_released);
    expect_operations(&result, expected, sizeof(expected) / sizeof(expected[0]));
}

static void test_initialization_failure_stops_chain(void)
{
    static const enum model_op expected[] = { MODEL_OP_INIT };
    struct model_result result;

    assert(model_run(MODEL_OP_INIT, &result) == 0);
    assert(result.error == -EIO);
    assert(result.done);
    assert(result.queue_empty);
    assert(result.storage_released);
    expect_operations(&result, expected, sizeof(expected) / sizeof(expected[0]));
}

int main(void)
{
    test_successful_callback_chain();
    test_io_failure_still_cleans_up();
    test_close_failure_is_not_misreported_as_clean();
    test_initialization_failure_stops_chain();
    puts("SPDK callback/queue model tests: PASS");
    return 0;
}
