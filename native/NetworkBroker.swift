import Foundation
import Darwin

// No listener: only the worker launched by this process receives a broker channel.
// Requests are data, never executable commands, file paths or proxy tunnels.
let input = FileHandle.standardInput
let output = FileHandle.standardOutput
let outputLock = NSLock()
let workerLock = NSLock()
let auditLock = NSLock()
// Requests beyond the concurrent allowance wait their turn instead of being refused, and every
// one of them can still be cancelled while it waits.
let queue = OperationQueue()
queue.maxConcurrentOperationCount = 16
let capacity = DispatchSemaphore(value: 64)
let capacityWaitSeconds = 20.0
var audit: FileHandle? = nil
var workerInput: FileHandle? = nil

// Requests waiting for a slot or in flight, each with the flag that abandons it.
let pendingLock = NSLock()
var pending = [String: UnsafeMutablePointer<Int32>]()

// Headers a caller may send, in the order they are sent. Cookies and the browser header families
// carry no credentials, and hiding them makes a browser identity contradict its own request.
// Credentials stay outside: `Authorization`, `Proxy-Authorization` and `Cookie2` are not in the
// list, because a delegated credential is the user's decision, not the page's.
//
// The JSON object arrives unordered, and an order that changes between identical requests is a
// signal no caller can explain, reproduce or cache. Emitting a fixed order - client hints first,
// Cookie last, roughly what a browser does - costs nothing and makes the bytes reproducible.
let forwardedRequestOrder = [
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "user-agent", "accept", "accept-language",
    "range", "if-modified-since", "if-none-match",
    "referer", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "priority", "content-type", "cookie",
]
let forwardedRequestHeaders = Set(forwardedRequestOrder)
let allowedMethods: Set<String> = ["GET", "HEAD", "POST"]
let allowedContentTypes: Set<String> = [
    "application/x-www-form-urlencoded", "application/json", "text/plain",
]
let maxRequestHeaders = 32
let maxRequestHeaderBytes = 1024
let maxCookieBytes = 8192
let maxRequestBodyBytes = 65_536
let maxResponseCookies = 12
let maxResponseCookieBytes = 8192

// Hop-by-hop and already-applied headers. Set-Cookie is deliberately absent: without it no
// caller can complete a token or challenge exchange, and the jar lives with the caller.
let droppedResponseHeaders: Set<String> = ["content-encoding", "content-length", "transfer-encoding",
                                           "connection", "keep-alive", "upgrade",
                                           "proxy-authenticate", "proxy-authorization", "trailer"]

// Failures the caller caused by asking for something the policy does not permit.
let policyErrors: Set<String> = [
    "Only GET, HEAD and POST are allowed", "Request body exceeds the size limit",
    "URL exceeds policy limits", "Invalid public URL", "Only public HTTP(S) is allowed",
    "URL credentials are blocked", "Only standard web ports are allowed", "Invalid hostname",
    "Non-public destination is blocked", "Request cancelled before it started",
]

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(("Web Search Network: " + message + "\n").utf8))
    exit(1)
}

func writeJSON(_ value: [String: Any], to handle: FileHandle, lock: NSLock) {
    guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else {
        fail("Invalid internal message")
    }
    lock.lock()
    defer { lock.unlock() }
    do { try handle.write(contentsOf: data + Data([10])) }
    catch { exit(0) }
}

func warn(_ message: String) {
    FileHandle.standardError.write(Data(("Web Search Network: warning: " + message + "\n").utf8))
}

func lines(_ handle: FileHandle, limit: Int, consume: (Data) -> Void) {
    var buffer = Data()
    var chunk = [UInt8](repeating: 0, count: 65536)
    while true {
        // FileHandle.read(upToCount:) can wait to fill its whole buffer on macOS.
        // A pipe frame must be processed immediately while the caller keeps stdin open.
        let count = Darwin.read(handle.fileDescriptor, &chunk, chunk.count)
        if count < 0 { if errno == EINTR { continue }; return }
        if count == 0 { break }
        buffer.append(contentsOf: chunk.prefix(count))
        while let end = buffer.firstIndex(of: 10) {
            if end > limit { fail("Message exceeds size limit") }
            consume(Data(buffer[..<end]))
            buffer.removeSubrange(...end)
        }
        if buffer.count > limit { fail("Message exceeds size limit") }
    }
}

func errorKind(_ message: String, tooLarge: Bool, cancelled: Bool) -> String {
    if tooLarge { return "too_large" }
    if cancelled { return "cancelled" }
    if message.hasPrefix("policy:") || policyErrors.contains(message) { return "policy" }
    return "transport"
}

// Everything about a request that the policy decides, computed before any transfer starts so
// the verdict can be tested without a network.
struct PreparedRequest {
    var headers = [String]()
    var forwarded = Set<String>()
    var filtered = Set<String>()
    var body: [UInt8]?
    var error: String?
}

func prepare(_ request: [String: Any]) -> PreparedRequest {
    var prepared = PreparedRequest()
    func reject(_ message: String) -> PreparedRequest {
        prepared.error = "policy: " + message
        prepared.headers = []
        prepared.forwarded = []
        prepared.filtered = []
        prepared.body = nil
        return prepared
    }
    guard let url = request["url"] as? String,
          let method = request["method"] as? String,
          !url.utf8.contains(0), !method.utf8.contains(0),
          Set(request.keys).isSubset(of: ["url", "method", "headers", "body"]) else {
        return reject("malformed request")
    }
    if !allowedMethods.contains(method) {
        return reject("method \(method) is not allowed")
    }
    if let text = request["body"] as? String {
        // Only POST may carry a body, and it stays small enough that a request is still a read.
        if method != "POST" { return reject("request bodies are allowed for POST only") }
        if text.utf8.contains(0) { return reject("request body contains a NUL byte") }
        let bytes = Array(text.utf8)
        if bytes.count > maxRequestBodyBytes {
            return reject("request body exceeds \(maxRequestBodyBytes) bytes")
        }
        prepared.body = bytes
    }

    let values = request["headers"] as? [String: String] ?? [:]
    // Group by normalised name first: two spellings of one header are refused together rather
    // than guessed at, and the order they arrived in is not the order they should leave in.
    var spellings: [String: [String]] = [:]
    for (key, value) in values {
        let name = nameOnly(key)
        guard key.utf8.count <= 64 else {
            prepared.filtered.insert(name)
            continue
        }
        spellings[name, default: []].append(value)
    }
    var contentType = ""
    for name in forwardedRequestOrder where name != "cookie" {
        let candidates = spellings[name] ?? []
        guard !candidates.isEmpty else { continue }
        guard candidates.count == 1, let value = candidates.first,
              value.utf8.count <= maxRequestHeaderBytes, !value.contains("\r"),
              !value.contains("\n"), !value.utf8.contains(0),
              prepared.headers.count < maxRequestHeaders else {
            prepared.filtered.insert(name)
            continue
        }
        if name == "content-type" { contentType = value }
        prepared.headers.append("\(name): \(value)")
        prepared.forwarded.insert(name)
    }
    // One Cookie line only, sent last as a browser sends it; a caller cannot smuggle several
    // spellings of the same header.
    if let cookie = spellings["cookie"], cookie.count == 1, let value = cookie.first,
       value.utf8.count <= maxCookieBytes, !value.contains("\r"), !value.contains("\n"),
       !value.utf8.contains(0) {
        prepared.headers.append("cookie: \(value)")
        prepared.forwarded.insert("cookie")
    } else if spellings["cookie"] != nil {
        prepared.filtered.insert("cookie")
    }
    // Anything the caller offered that did not leave - unknown, refused or over the limit - is
    // reported by name, because "filtered" that omits the silent drops explains nothing.
    for name in spellings.keys where !prepared.forwarded.contains(name) {
        prepared.filtered.insert(name)
    }
    if method == "POST" {
        let subtype = contentType.split(separator: ";").first.map(String.init)?
            .trimmingCharacters(in: .whitespaces).lowercased() ?? ""
        if !allowedContentTypes.contains(subtype) {
            return reject("POST content type is not allowed")
        }
    }
    return prepared
}

func fetch(_ request: [String: Any],
           cancel: UnsafeMutablePointer<Int32>? = nil) -> [String: Any] {
    guard let url = request["url"] as? String,
          let method = request["method"] as? String else {
        return ["error": "policy: malformed request", "error_kind": "policy"]
    }
    let prepared = prepare(request)
    if let error = prepared.error {
        return ["error": error, "error_kind": "policy"]
    }

    var response: WSResponse
    if let body = prepared.body {
        response = body.withUnsafeBufferPointer { buffer in
            ws_fetch(url, method, prepared.headers.joined(separator: "\n"),
                     buffer.baseAddress.map { UnsafeRawPointer($0).assumingMemoryBound(to: CChar.self) },
                     body.count, cancel)
        }
    } else {
        response = ws_fetch(url, method, prepared.headers.joined(separator: "\n"), nil, 0, cancel)
    }
    defer { ws_free(&response) }
    let cancelled = cancel.map { $0.pointee != 0 } ?? false

    var result: [String: Any]
    if let error = response.error {
        let message = String(cString: error)
        result = ["error": message,
                  "error_kind": errorKind(message, tooLarge: response.too_large != 0,
                                          cancelled: cancelled)]
    } else {
        var incoming = [[String]]()
        var cookieBytes = 0
        var cookies = 0
        var dropped = Set<String>()
        if let raw = response.headers {
            for line in String(cString: raw).components(separatedBy: "\r\n") {
                if line.hasPrefix("HTTP/") { incoming.removeAll(); continue }
                guard let colon = line.firstIndex(of: ":") else { continue }
                let key = String(line[..<colon]).lowercased()
                let value = String(line[line.index(after: colon)...])
                    .trimmingCharacters(in: .whitespaces)
                if droppedResponseHeaders.contains(key) { dropped.insert(key); continue }
                if key == "set-cookie" {
                    // A challenge or session cookie has to survive the trip, but a page cannot
                    // use one response to stream an unbounded amount of state into the caller.
                    cookieBytes += value.utf8.count
                    cookies += 1
                    if cookies > maxResponseCookies || cookieBytes > maxResponseCookieBytes {
                        dropped.insert("set-cookie")
                        continue
                    }
                }
                incoming.append([key, value])
            }
        }
        let data = response.body.map { Data(bytes: $0, count: response.body_size) } ?? Data()
        result = ["status": response.status, "headers": incoming,
                  "body": data.base64EncodedString()]
        // Names only, never values: this is what the caller needs to explain an empty page.
        var filteredNames = Array(prepared.filtered).sorted()
        filteredNames.append(contentsOf: Array(dropped).sorted().map { "-\($0)" })
        if !filteredNames.isEmpty { result["filtered"] = filteredNames }
        if !prepared.forwarded.isEmpty { result["forwarded"] = prepared.forwarded.sorted() }
    }
    if let log = audit {
        let host = URLComponents(string: url)?.host ?? "invalid"
        // Host and method only. Paths, queries, header values and contents are never logged.
        writeJSON(["time": ISO8601DateFormatter().string(from: Date()), "host": host,
                   "method": method, "status": response.status, "bytes": response.body_size,
                   "error": result["error"] ?? NSNull(),
                   "error_kind": result["error_kind"] ?? NSNull(),
                   "filtered": result["filtered"] ?? NSNull()],
                  to: log, lock: auditLock)
    }
    return result
}

func nameOnly(_ key: String) -> String {
    String(key.lowercased().prefix(while: { $0 != " " && $0 != "\t" }))
}

func pendingFlag(_ id: String) -> UnsafeMutablePointer<Int32>? {
    pendingLock.lock()
    defer { pendingLock.unlock() }
    return pending[id]
}

func releasePending(_ id: String) {
    pendingLock.lock()
    let flag = pending.removeValue(forKey: id)
    pendingLock.unlock()
    flag?.deallocate()
    capacity.signal()
}

signal(SIGPIPE, SIG_IGN)
let userHome = NSHomeDirectory()
// In particular, libcurl's internal DNS-over-HTTPS handles must not inherit proxies
// or alternate trust stores from Pi's environment.
let clearEnvironment: Void = {
    let environment = ProcessInfo.processInfo.environment
    for key in environment.keys { unsetenv(key) }
}()
ws_init()
let args = CommandLine.arguments
if args.count == 2 && args[1] == "--self-test" {
    print("Native transport: \(String(cString: ws_version()))")
    for ip in ["127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254", "198.18.0.1",
               "192.0.2.1", "224.0.0.1", "::1", "::ffff:8.8.8.8", "2001:db8::1", "2002::1"] {
        if ws_public_ip(ip) != 0 { fail("Private-address policy failed") }
    }
    for ip in ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"] {
        if ws_public_ip(ip) != 1 { fail("Public-address policy failed") }
    }
    for request: [String: Any] in [
        ["url": "file:///etc/passwd", "method": "GET"],
        ["url": "http://127.0.0.1", "method": "GET"],
        ["url": "https://user:pass@example.com", "method": "GET"],
        ["url": "https://example.com:8443", "method": "GET"],
        ["url": "https://example.com", "method": "PUT"],
        ["url": "https://example.com", "method": "GET", "body": "secret"],
        ["url": "https://example.com", "method": "POST"],
        ["url": "https://example.com", "method": "POST", "body": "a=1",
         "headers": ["content-type": "text/html"]],
        ["url": "https://example.com", "method": "POST", "body": String(repeating: "x", count: 70_000),
         "headers": ["content-type": "application/json"]],
        ["url": "https://example.com", "method": "PUT", "body": "a=1",
         "headers": ["content-type": "application/json"]]
    ] {
        if fetch(request)["error_kind"] as? String != "policy" {
            fail("Request policy accepted \(request["method"] ?? "?") \(request["url"] ?? "?")")
        }
    }
    // Cookies and browser headers travel; credentials do not, and the caller is told which is
    // which. These assertions must not need a network, so only the policy verdict is checked.
    let probe = prepare(["url": "https://example.com", "method": "GET",
                         "headers": ["cookie": "session=1",
                                     "referer": "https://example.com/",
                                     "authorization": "Bearer secret", "x-api-key": "secret",
                                     "sec-fetch-mode": "cors"]])
    guard probe.error == nil, probe.filtered.isSuperset(of: ["authorization", "x-api-key"]),
          probe.forwarded.isSuperset(of: ["cookie", "referer", "sec-fetch-mode"]),
          probe.headers.last == "cookie: session=1" else {
        fail("Header policy failed: \(probe)")
    }
    let oversizedValue = prepare(["url": "https://example.com", "method": "GET",
                                  "headers": ["user-agent": String(repeating: "u", count: 2048)]])
    guard oversizedValue.filtered.contains("user-agent"), oversizedValue.headers.isEmpty else {
        fail("Header size policy failed")
    }
    let allowedPost = prepare(["url": "https://example.com", "method": "POST", "body": "a=1",
                               "headers": ["content-type": "application/x-www-form-urlencoded"]])
    guard allowedPost.error == nil, allowedPost.body?.count == 3,
          allowedPost.forwarded.contains("content-type") else {
        fail("POST policy failed: \(allowedPost)")
    }
    let flag = UnsafeMutablePointer<Int32>.allocate(capacity: 1)
    flag.pointee = 1
    if fetch(["url": "https://example.com", "method": "GET"], cancel: flag)["error_kind"]
        as? String != "cancelled" { fail("Cancellation policy failed") }
    flag.deallocate()
    print("PASS native destination, method, body, header, credential and cancellation policy")
    exit(0)
}

// Test mode exercises the actual native policy and TLS implementation, without a container.
if args.count == 2 && args[1] == "--test-transport" {
    var error: UnsafeMutablePointer<CChar>?
    if ws_sandbox(nil, &error) != 0 { fail("Cannot apply broker filesystem restrictions") }
    lines(input, limit: 32768) { data in
        guard let request = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { fail("Invalid test request") }
        writeJSON(fetch(request), to: output, lock: outputLock)
    }
    exit(0)
}

if args.count == 3 && args[1] == "--check-sandbox-file" {
    var error: UnsafeMutablePointer<CChar>?
    if ws_sandbox(nil, &error) != 0 { fail("Cannot apply broker filesystem restrictions") }
    if let file = FileHandle(forReadingAtPath: args[2]) {
        try? file.close()
        fail("Broker could open a file outside its allowed system paths")
    }
    print("PASS broker cannot read private files")
    exit(0)
}

guard args.count == 6 else { fail("Use the web-search-isolated launcher") }
let docker = args[1], image = args[2], volume = args[3], auditPath = args[4]
let dockerEndpoint = args[5]
guard docker.hasPrefix("/"), image.hasPrefix("sha256:"),
      dockerEndpoint.hasPrefix("unix:///"), !dockerEndpoint.utf8.contains(0),
      volume.range(of: "^[a-zA-Z0-9][a-zA-Z0-9_.-]+$", options: .regularExpression) != nil else {
    fail("Invalid installed runtime configuration")
}
if !FileManager.default.fileExists(atPath: auditPath) {
    guard FileManager.default.createFile(atPath: auditPath, contents: nil,
                                        attributes: [.posixPermissions: 0o600]) else {
        fail("Cannot create audit log")
    }
}
guard let log = FileHandle(forWritingAtPath: auditPath) else { fail("Cannot open audit log") }
_ = try? log.seekToEnd()
audit = log
let process = Process()
process.executableURL = URL(fileURLWithPath: docker)
let dockerConfig = URL(fileURLWithPath: auditPath).deletingLastPathComponent()
    .appendingPathComponent("docker-client", isDirectory: true)
do {
    try FileManager.default.createDirectory(at: dockerConfig, withIntermediateDirectories: true,
                                            attributes: [.posixPermissions: 0o700])
    try Data("{}".utf8).write(to: dockerConfig.appendingPathComponent("config.json"))
} catch { fail("Cannot prepare the local Docker client configuration") }
process.arguments = ["--host", dockerEndpoint, "--config", dockerConfig.path,
    "run", "--rm", "--pull=never", "--interactive", "--init", "--network=none",
    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256",
    "--memory=2g", "--cpus=4", "--user=1000:1000", "--shm-size=256m",
    "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,uid=1000,gid=1000,mode=1777",
    "--mount=type=volume,src=\(volume),dst=/data", image]
// Deliberate allowlist: no proxy credentials, API keys, Python hooks or shell startup files.
let childEnv = ["PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": userHome]
process.environment = childEnv
let toWorker = Pipe(), fromWorker = Pipe()
process.standardInput = toWorker
process.standardOutput = fromWorker
process.standardError = FileHandle.standardError
workerInput = toWorker.fileHandleForWriting
do { try process.run() } catch { fail("Cannot start the isolated Docker runtime: \(error)") }
var sandboxError: UnsafeMutablePointer<CChar>?
if ws_sandbox(auditPath, &sandboxError) != 0 {
    process.terminate()
    fail("Cannot apply broker filesystem restrictions; refusing to continue")
}
DispatchQueue.global().async {
    lines(input, limit: 1_000_000) { data in
        guard let line = String(data: data, encoding: .utf8),
              (try? JSONSerialization.jsonObject(with: data)) != nil else {
            // One unreadable frame must not end every request already in flight.
            warn("Ignoring malformed MCP frame")
            return
        }
        writeJSON(["kind": "mcp", "data": line], to: toWorker.fileHandleForWriting,
                  lock: workerLock)
    }
    try? toWorker.fileHandleForWriting.close()
}
lines(fromWorker.fileHandleForReading, limit: 16_000_000) { data in
    guard let frame = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
          let kind = frame["kind"] as? String else {
        warn("Ignoring malformed worker frame")
        return
    }
    if kind == "mcp", let line = frame["data"] as? String {
        outputLock.lock()
        output.write(Data((line + "\n").utf8))
        outputLock.unlock()
    } else if kind == "fetch",
              let id = frame["id"] as? String, id.utf8.count <= 64,
              let request = frame["request"] as? [String: Any] {
        let flag = UnsafeMutablePointer<Int32>.allocate(capacity: 1)
        flag.pointee = 0
        pendingLock.lock()
        pending[id] = flag
        pendingLock.unlock()
        // Wait for a slot rather than refusing the request; a page render needs many of them.
        guard capacity.wait(timeout: .now() + capacityWaitSeconds) == .success else {
            pendingLock.lock()
            pending.removeValue(forKey: id)
            pendingLock.unlock()
            flag.deallocate()
            writeJSON(["kind": "fetch_result", "id": id,
                       "result": ["error": "Network broker is saturated",
                                  "error_kind": "transport"]],
                      to: toWorker.fileHandleForWriting, lock: workerLock)
            return
        }
        queue.addOperation {
            defer { releasePending(id) }
            let flag = pendingFlag(id)
            let result: [String: Any] = flag == nil
                ? ["error": "Request cancelled before it started", "error_kind": "cancelled"]
                : fetch(request, cancel: flag)
            writeJSON(["kind": "fetch_result", "id": id, "result": result],
                      to: toWorker.fileHandleForWriting, lock: workerLock)
        }
    } else if kind == "cancel", let id = frame["id"] as? String {
        pendingLock.lock()
        pending[id]?.pointee = 1
        pendingLock.unlock()
    } else { warn("Ignoring unknown worker message") }
}
queue.cancelAllOperations()
process.waitUntilExit()
exit(process.terminationStatus)
