/* Experimental native traversal of the exact buffer used by PackedIndex.
 * Trusted, in-process compiler output only: not a parser for untrusted buffers.
 * Every call owns scratch memory; the input buffer is immutable and shareable.
 */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

typedef struct { uint32_t node; size_t position; } Work;

static int compare_id(const void *a, const void *b) {
    uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return (x > y) - (x < y);
}

int64_t packed_select(const void *buffer, const char *path, size_t length,
                      uint32_t *out, size_t capacity) {
    const uint32_t *w = buffer;
    uint32_t n = w[0], e = w[1], ns = w[4], es = w[5], ids = w[6];
    const char *labels = (const char *)buffer + w[7];
    Work *stack = malloc((size_t)n * sizeof(Work));
    if (!stack) return -1;
    size_t pending = 1, count = 0;
    stack[0] = (Work){0, 0};
    while (pending) {
        Work current = stack[--pending];
        uint32_t node = current.node;
        uint32_t start = w[ns + 3 * n + node], size = w[ns + 4 * n + node];
        if (size > capacity - count) { free(stack); return -1; }
        memcpy(out + count, w + ids + start, (size_t)size * sizeof(uint32_t));
        count += size;
        /* length+1 signals that the final segment, including an empty one,
           was consumed. A trailing slash is distinct from no trailing slash. */
        if (current.position > length) {
            start = w[ns + 5 * n + node]; size = w[ns + 6 * n + node];
            if (size > capacity - count) { free(stack); return -1; }
            memcpy(out + count, w + ids + start, (size_t)size * sizeof(uint32_t));
            count += size;
            continue;
        }
        size_t end = current.position;
        while (end < length && path[end] != '/') ++end;
        uint32_t wildcard = w[ns + 2 * n + node];
        if (wildcard != UINT32_MAX) stack[pending++] = (Work){wildcard, end + 1};
        start = w[ns + node]; size = w[ns + n + node];
        for (uint32_t edge = start; edge < start + size; ++edge) {
            if (w[es + e + edge] == end - current.position &&
                memcmp(labels + w[es + edge], path + current.position, end - current.position) == 0) {
                stack[pending++] = (Work){w[es + 2 * e + edge], end + 1};
            }
        }
    }
    free(stack);
    qsort(out, count, sizeof(uint32_t), compare_id);
    return (int64_t)count;
}
