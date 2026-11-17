import Foundation
import Darwin

// No listener: only the worker launched by this process receives a broker channel.
// Requests are data, never executable commands, file paths or proxy tunnels.
let input = FileHandle.standardInput
let output = FileHandle.standardOutput
let outputLock = NSLock()
let workerLock = NSLock()
let auditLock = NSLock()
let queue = OperationQueue()
queue.maxConcurrentOperationCount = 8
let capacity = DispatchSemaphore(value: 24)
var audit: FileHandle? = nil
var workerInput: FileHandle? = nil

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

func fetch(_ request: [String: Any]) -> [String: Any] {
    guard let url = request["url"] as? String,
          let method = request["method"] as? String,
          !url.utf8.contains(0), !method.utf8.contains(0),
          Set(request.keys).isSubset(of: ["url", "method", "headers"]) else {
        return ["error": "Malformed request or request body is blocked"]
    }
    let allowed: Set<String> = ["accept", "accept-language", "user-agent", "range",
                                "if-modified-since", "if-none-match"]
    var headers = [String]()
    if let values = request["headers"] as? [String: String] {
        for (key, value) in values {
            if allowed.contains(key.lowercased()) && key.utf8.count <= 64 &&
               value.utf8.count <= 1024 && !value.contains("\r") && !value.contains("\n") &&
               !value.utf8.contains(0) {
                headers.append("\(key): \(value)")
            }
        }
    }
    var response = ws_fetch(url, method, headers.joined(separator: "\n"))
    defer { ws_free(&response) }
    var result: [String: Any]
    if let error = response.error {
        result = ["error": String(cString: error)]
    } else {
        var incoming = [[String]]()
        let dropped: Set<String> = ["content-encoding", "content-length", "transfer-encoding",
                                     "connection", "keep-alive", "set-cookie", "upgrade",
                                     "proxy-authenticate", "proxy-authorization", "trailer"]
        if let raw = response.headers {
            for line in String(cString: raw).components(separatedBy: "\r\n") {
                if line.hasPrefix("HTTP/") { incoming.removeAll(); continue }
                guard let colon = line.firstIndex(of: ":") else { continue }
                let key = String(line[..<colon]).lowercased()
                if !dropped.contains(key) {
                    let value = String(line[line.index(after: colon)...])
                        .trimmingCharacters(in: .whitespaces)
                    incoming.append([key, value])
                }
            }
        }
        let body = response.body.map { Data(bytes: $0, count: response.body_size) } ?? Data()
        result = ["status": response.status, "headers": incoming, "body": body.base64EncodedString()]
    }
    if let log = audit {
        let host = URLComponents(string: url)?.host ?? "invalid"
        // Host and method only. Paths, queries, headers and contents are never logged.
        writeJSON(["time": ISO8601DateFormatter().string(from: Date()), "host": host,
                   "method": method, "status": response.status, "bytes": response.body_size,
                   "error": result["error"] ?? NSNull()], to: log, lock: auditLock)
    }
    return result
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
    for ip in ["127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254", "198.18.0.1",
               "192.0.2.1", "224.0.0.1", "::1", "::ffff:8.8.8.8", "2001:db8::1", "2002::1"] {
        if ws_public_ip(ip) != 0 { fail("Private-address policy failed") }
    }
    for ip in ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"] {
        if ws_public_ip(ip) != 1 { fail("Public-address policy failed") }
    }
    for request: [String: Any] in [
        ["url": "https://example.com", "method": "POST"],
        ["url": "file:///etc/passwd", "method": "GET"],
        ["url": "http://127.0.0.1", "method": "GET"],
        ["url": "https://user:pass@example.com", "method": "GET"],
        ["url": "https://example.com:8443", "method": "GET"],
        ["url": "https://example.com", "method": "GET", "body": "secret"]
    ] {
        if fetch(request)["error"] == nil { fail("Request policy failed") }
    }
    print("PASS native destination, method, body, scheme and credential policy")
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
            fail("Invalid MCP input")
        }
        writeJSON(["kind": "mcp", "data": line], to: toWorker.fileHandleForWriting,
                  lock: workerLock)
    }
    try? toWorker.fileHandleForWriting.close()
}
lines(fromWorker.fileHandleForReading, limit: 16_000_000) { data in
    guard let frame = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    else { fail("Invalid worker message") }
    if frame["kind"] as? String == "mcp", let line = frame["data"] as? String {
        outputLock.lock()
        output.write(Data((line + "\n").utf8))
        outputLock.unlock()
    } else if frame["kind"] as? String == "fetch",
              let id = frame["id"] as? String, id.utf8.count <= 64,
              let request = frame["request"] as? [String: Any] {
        if capacity.wait(timeout: .now()) != .success {
            writeJSON(["kind": "fetch_result", "id": id,
                       "result": ["error": "Network broker capacity exceeded"]],
                      to: toWorker.fileHandleForWriting, lock: workerLock)
        } else {
            queue.addOperation {
                defer { capacity.signal() }
                writeJSON(["kind": "fetch_result", "id": id, "result": fetch(request)],
                          to: toWorker.fileHandleForWriting, lock: workerLock)
            }
        }
    } else { fail("Unknown worker message") }
}
queue.cancelAllOperations()
process.waitUntilExit()
exit(process.terminationStatus)
