// The only internet-capable implementation. Public web destinations, no credentials, bounded
// bodies. The caller may carry cookies and browser request headers; this file holds no state.
#include "broker.h"
#include <arpa/inet.h>
#include <curl/curl.h>
#include <netdb.h>
#include <sandbox.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_BODY 5000000
#define MAX_REQUEST_BODY 65536
#define MAX_HEADERS 65536

// Two independent resolvers. A single DNS-over-HTTPS endpoint is a single point of failure for
// every destination, and an unreachable resolver looks like an unreachable website.
static const char *const DOH_URLS[] = {"https://1.1.1.1/dns-query",
                                       "https://dns.quad9.net/dns-query"};

void ws_init(void) { curl_global_init(CURL_GLOBAL_DEFAULT); }

const char *ws_version(void) { return curl_version(); }

int ws_public_ip(const char *address) {
    struct in_addr v4;
    struct in6_addr v6;
    if (inet_pton(AF_INET, address, &v4) == 1) {
        unsigned int ip = ntohl(v4.s_addr);
        unsigned int a = ip >> 24, b = (ip >> 16) & 255, c = (ip >> 8) & 255;
        if (a == 0 || a == 10 || a == 127 || a >= 224) return 0;
        if (a == 100 && b >= 64 && b <= 127) return 0;
        if (a == 169 && b == 254) return 0;
        if (a == 172 && b >= 16 && b <= 31) return 0;
        if (a == 192 && (b == 168 || (b == 0 && (c == 0 || c == 2)) ||
                        (b == 88 && c == 99))) return 0;
        if (a == 198 && (b == 18 || b == 19 || (b == 51 && c == 100))) return 0;
        if (a == 203 && b == 0 && c == 113) return 0;
        return 1;
    }
    if (inet_pton(AF_INET6, address, &v6) == 1) {
        const unsigned char *b = v6.s6_addr;
        // Only global unicast; exclude special-purpose, documentation and 6to4 space.
        if ((b[0] & 0xe0) != 0x20) return 0;
        if (b[0] == 0x20 && b[1] == 0x01 && b[2] <= 1) return 0;
        if (b[0] == 0x20 && b[1] == 0x01 && b[2] == 0x0d && b[3] == 0xb8) return 0;
        if (b[0] == 0x20 && b[1] == 0x02) return 0;
        if (b[0] == 0x3f && b[1] == 0xff && (b[2] & 0xf0) == 0) return 0;
        return 1;
    }
    return 0;
}

static size_t body_callback(char *data, size_t size, size_t count, void *context) {
    WSResponse *r = context;
    size_t n = size * count;
    // Aborting the write is the only way to bound a chunked response, so record the reason.
    if (n > MAX_BODY || r->body_size > MAX_BODY - n) {
        r->too_large = 1;
        return 0;
    }
    unsigned char *p = realloc(r->body, r->body_size + n + 1);
    if (!p) return 0;
    r->body = p;
    memcpy(p + r->body_size, data, n);
    r->body_size += n;
    p[r->body_size] = 0;
    return n;
}

static size_t header_callback(char *data, size_t size, size_t count, void *context) {
    WSResponse *r = context;
    size_t n = size * count;
    if (n > MAX_HEADERS || r->headers_size > MAX_HEADERS - n) return 0;
    char *p = realloc(r->headers, r->headers_size + n + 1);
    if (!p) return 0;
    r->headers = p;
    memcpy(p + r->headers_size, data, n);
    r->headers_size += n;
    p[r->headers_size] = 0;
    return n;
}

static int cancel_callback(void *context, curl_off_t total, curl_off_t now,
                           curl_off_t upload_total, curl_off_t uploaded) {
    (void)total; (void)now; (void)upload_total; (void)uploaded;
    const volatile int *cancel = ((WSResponse *)context)->cancel;
    return cancel && *cancel ? 1 : 0;
}

static curl_socket_t public_socket(void *context, curlsocktype purpose,
                                   struct curl_sockaddr *address) {
    (void)context; (void)purpose;
    char ip[INET6_ADDRSTRLEN];
    const void *raw = NULL;
    if (address->family == AF_INET)
        raw = &((struct sockaddr_in *)&address->addr)->sin_addr;
    else if (address->family == AF_INET6)
        raw = &((struct sockaddr_in6 *)&address->addr)->sin6_addr;
    if (!raw || !inet_ntop(address->family, raw, ip, sizeof(ip)) || !ws_public_ip(ip))
        return CURL_SOCKET_BAD;
    return socket(address->family, address->socktype, address->protocol);
}

WSResponse ws_fetch(const char *url, const char *method, const char *headers, const char *body,
                    size_t body_size, const volatile int *cancel) {
    WSResponse r = {0};
    CURLU *parsed = curl_url();
    CURL *curl = NULL;
    char *scheme = NULL, *host = NULL, *port = NULL, *user = NULL, *password = NULL;
    struct curl_slist *out_headers = NULL;
    const char *error = NULL;
    char curl_error[CURL_ERROR_SIZE] = {0};
    char *header_copy = NULL;
    r.cancel = cancel;

    if (strcmp(method, "GET") && strcmp(method, "HEAD") && strcmp(method, "POST")) {
        error = "Only GET, HEAD and POST are allowed"; goto done;
    }
    if (body_size > MAX_REQUEST_BODY) { error = "Request body exceeds the size limit"; goto done; }
    if (cancel && *cancel) { error = "Request cancelled before it started"; goto done; }
    if (strlen(url) > 4096 || strpbrk(url, "\r\n\t")) {
        error = "URL exceeds policy limits"; goto done;
    }
    if (!parsed || curl_url_set(parsed, CURLUPART_URL, url, 0) != CURLUE_OK ||
        curl_url_get(parsed, CURLUPART_SCHEME, &scheme, 0) != CURLUE_OK ||
        curl_url_get(parsed, CURLUPART_HOST, &host, 0) != CURLUE_OK) {
        error = "Invalid public URL"; goto done;
    }
    if (strcmp(scheme, "http") && strcmp(scheme, "https")) {
        error = "Only public HTTP(S) is allowed"; goto done;
    }
    if (curl_url_get(parsed, CURLUPART_USER, &user, 0) == CURLUE_OK ||
        curl_url_get(parsed, CURLUPART_PASSWORD, &password, 0) == CURLUE_OK) {
        error = "URL credentials are blocked"; goto done;
    }
    curl_url_get(parsed, CURLUPART_PORT, &port, CURLU_DEFAULT_PORT);
    if (!port || strcmp(port, !strcmp(scheme, "https") ? "443" : "80")) {
        error = "Only standard web ports are allowed"; goto done;
    }
    char dns_host[256];
    if (strlen(host) > 253) { error = "Invalid hostname"; goto done; }
    snprintf(dns_host, sizeof(dns_host), "%s", host);
    size_t length = strlen(dns_host);
    if (dns_host[0] == '[' && length > 2 && dns_host[length - 1] == ']') {
        memmove(dns_host, dns_host + 1, length - 2); dns_host[length - 2] = 0;
    }
    struct in_addr v4;
    struct in6_addr v6;
    if ((inet_pton(AF_INET, dns_host, &v4) == 1 ||
         inet_pton(AF_INET6, dns_host, &v6) == 1) && !ws_public_ip(dns_host)) {
        error = "Non-public destination is blocked"; goto done;
    }
    curl = curl_easy_init();
    if (!curl) { error = "Network allocation failed"; goto done; }
    header_copy = strdup(headers);
    if (!header_copy) { error = "Network allocation failed"; goto done; }
    char *save = NULL;
    for (char *line = strtok_r(header_copy, "\n", &save); line; line = strtok_r(NULL, "\n", &save))
        out_headers = curl_slist_append(out_headers, line);

    curl_easy_setopt(curl, CURLOPT_URL, url);
    // Fixed-address, certificate-verified DoH avoids local/TUN synthetic DNS and never
    // consults LAN DNS for submitted names. The socket hook validates the ACTUAL peer
    // before connect(), covering redirects, rebinding, and every address curl tries.
    curl_easy_setopt(curl, CURLOPT_DOH_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_DOH_SSL_VERIFYHOST, 2L);
    curl_easy_setopt(curl, CURLOPT_OPENSOCKETFUNCTION, public_socket);
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, out_headers);
    curl_easy_setopt(curl, CURLOPT_PROXY, "");
    curl_easy_setopt(curl, CURLOPT_NETRC, CURL_NETRC_IGNORED);
    curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "http,https");
    curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS_STR, "http,https");
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 10L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 45L);
    curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, body_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &r);
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, header_callback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &r);
    // The only abort path: the caller can give up on a transfer without a second channel.
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, cancel_callback);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &r);
    curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, curl_error);
    if (body && body_size) {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, body);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, (long)body_size);
    } else if (!strcmp(method, "POST")) {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, 0L);
    }
    if (!strcmp(method, "HEAD")) curl_easy_setopt(curl, CURLOPT_NOBODY, 1L);
    CURLcode result = CURLE_OK;
    for (size_t attempt = 0; attempt < sizeof(DOH_URLS) / sizeof(*DOH_URLS); attempt++) {
        curl_easy_setopt(curl, CURLOPT_DOH_URL, DOH_URLS[attempt]);
        curl_error[0] = 0;
        result = curl_easy_perform(curl);
        // A resolver that cannot answer is retried through the other operator, but only while
        // nothing has been consumed, so a partial body is never silently restarted.
        if (result == CURLE_COULDNT_RESOLVE_HOST && !r.body_size && !r.too_large &&
            !(cancel && *cancel) && attempt + 1 < sizeof(DOH_URLS) / sizeof(*DOH_URLS))
            continue;
        break;
    }
    if (result != CURLE_OK) {
        if (r.too_large) {
            // Avoid logging URLs, credentials or response content in errors.
            error = "Response exceeds the native byte limit";
        } else if (result == CURLE_ABORTED_BY_CALLBACK) {
            error = "Request cancelled by client";
        } else {
            error = curl_easy_strerror(result);
        }
        goto done;
    }
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &r.status);
done:
    if (error) r.error = strdup(error);
    if (curl) curl_easy_cleanup(curl);
    if (parsed) curl_url_cleanup(parsed);
    curl_slist_free_all(out_headers);
    curl_free(scheme); curl_free(host); curl_free(port); curl_free(user); curl_free(password);
    free(header_copy);
    return r;
}

void ws_free(WSResponse *r) { free(r->body); free(r->headers); free(r->error); }

int ws_sandbox(const char *audit_path, char **error) {
    // Applied after spawning the fixed Docker command. The broker itself cannot read
    // user files, spawn commands, or write files; audit output is a pre-opened descriptor.
    const char *profile =
        "(version 1)(allow default)"
        "(deny file-read* file-write* process-exec process-fork)"
        "(allow file-read-metadata)"
        "(allow file-read* (subpath \"/System\") (subpath \"/usr/lib\")"
        " (subpath \"/usr/share\") (subpath \"/private/etc\") (subpath \"/etc\")"
        " (subpath \"/private/var/run\") (subpath \"/Library/Preferences/SystemConfiguration\")"
        " (subpath \"/Library/Apple\")"
        " (literal \"/dev/urandom\") (literal \"/dev/random\"))";
    if (!audit_path) return sandbox_init(profile, 0, error);
    char *escaped = calloc(strlen(audit_path) * 2 + 1, 1);
    if (!escaped) return -1;
    size_t j = 0;
    for (size_t i = 0; audit_path[i]; i++) {
        if (audit_path[i] == '\\' || audit_path[i] == '"') escaped[j++] = '\\';
        escaped[j++] = audit_path[i];
    }
    char *with_audit = NULL;
    if (asprintf(&with_audit, "%s(allow file-write-data (literal \"%s\"))", profile, escaped) < 0) {
        free(escaped); return -1;
    }
    int result = sandbox_init(with_audit, 0, error);
    free(escaped); free(with_audit);
    return result;
}
