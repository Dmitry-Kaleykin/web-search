#ifndef WEB_SEARCH_BROKER_H
#define WEB_SEARCH_BROKER_H
#include <stddef.h>
typedef struct {
    unsigned char *body;
    size_t body_size;
    char *headers;
    size_t headers_size;
    long status;
    char *error;
    // Diagnostics for the caller: why a transfer stopped, and the abort flag it may set.
    int too_large;
    const volatile int *cancel;
} WSResponse;
void ws_init(void);
const char *ws_version(void);
WSResponse ws_fetch(const char *url, const char *method, const char *headers, const char *body,
                    size_t body_size, const volatile int *cancel);
void ws_free(WSResponse *response);
int ws_public_ip(const char *address);
int ws_sandbox(const char *audit_path, char **error);
#endif
