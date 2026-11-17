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
} WSResponse;
void ws_init(void);
WSResponse ws_fetch(const char *url, const char *method, const char *headers);
void ws_free(WSResponse *response);
int ws_public_ip(const char *address);
int ws_sandbox(const char *audit_path, char **error);
#endif
