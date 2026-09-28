"""Measure the TLS ClientHello and HTTP/2 frame sequence this tool sends.

Runs a local TLS server, parses the ClientHello from raw bytes before completing the handshake
(memory BIO), then logs the HTTP/2 frame order, SETTINGS, WINDOW_UPDATE and header field order.

    .venv/bin/python tools/transport_fingerprint.py compare
    .venv/bin/python tools/transport_fingerprint.py chromium | chromium-channel | real-http

Needs `cryptography` and `hpack`, both already present through the `browser` extra and
`httpx[http2]`; nothing here is imported by the server.

Modes: `compare` (all readers side by side), `httpx-h1` / `httpx-h2` (a bare client, to show what
the transport switch changes), `chromium` (the browser reader), `chromium-channel` (the same with
`channel=chromium`), `real-http` (the project's own HTTPReader class). The server binds
127.0.0.1 on an ephemeral port and serves only itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import ipaddress
import json
import ssl
import struct
import sys
import tempfile
from pathlib import Path

# Runs from any working directory: the package lives in src/ next to this file's parent.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import hpack
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
FRAME_NAMES = {0x0: "DATA", 0x1: "HEADERS", 0x2: "PRIORITY", 0x4: "SETTINGS", 0x8: "WINDOW_UPDATE"}
SETTINGS_NAMES = {
    1: "HEADER_TABLE_SIZE",
    2: "ENABLE_PUSH",
    3: "MAX_CONCURRENT_STREAMS",
    4: "INITIAL_WINDOW_SIZE",
    5: "MAX_FRAME_SIZE",
    6: "MAX_HEADER_LIST_SIZE",
}


def is_grease(value: int) -> bool:
    """TLS GREASE values repeat one byte whose low nibble is 0xA, e.g. 0x6a6a."""
    low, high = value & 0xFF, (value >> 8) & 0xFF
    return high == low and (low & 0x0F) == 0x0A


def parse_client_hello(data: bytes) -> dict:
    """Parse the outer ClientHello from raw TLS record bytes."""
    out: dict = {}
    if len(data) < 5 or data[0] != 0x16:
        return {"error": "first record was not a TLS handshake"}
    record_len = struct.unpack("!H", data[3:5])[0]
    body = data[5 : 5 + record_len]
    if len(body) < 5 or body[0] != 0x01:
        return {"error": "first handshake message was not a ClientHello"}
    hello_len = struct.unpack("!I", b"\x00" + body[1:4])[0]
    b = body[4 : 4 + hello_len]
    offset = 2 + 32  # client_version + random
    sid_len = b[offset]
    offset += 1 + sid_len
    cipher_bytes = struct.unpack("!H", b[offset : offset + 2])[0]
    offset += 2
    ciphers = list(struct.unpack(f"!{cipher_bytes // 2}H", b[offset : offset + cipher_bytes]))
    offset += cipher_bytes
    offset += 1 + b[offset]  # compression methods
    extension_bytes = struct.unpack("!H", b[offset : offset + 2])[0]
    offset += 2
    extensions, alpn, groups, sigalgs, versions, key_share = [], [], [], [], [], []
    end = offset + extension_bytes
    while offset + 4 <= end:
        etype = struct.unpack("!H", b[offset : offset + 2])[0]
        elen = struct.unpack("!H", b[offset + 2 : offset + 4])[0]
        payload = b[offset + 4 : offset + 4 + elen]
        extensions.append(etype)
        if etype == 10:  # supported_groups
            count = struct.unpack("!H", payload[0:2])[0]
            groups = list(struct.unpack(f"!{count // 2}H", payload[2 : 2 + count]))
        elif etype == 13:  # signature_algorithms
            count = struct.unpack("!H", payload[0:2])[0]
            sigalgs = list(struct.unpack(f"!{count // 2}H", payload[2 : 2 + count]))
        elif etype == 16:  # protocol_information (ALPN)
            position = 2
            while position < len(payload):
                #   length-prefixed protocol name
                name_len = payload[position]
                alpn.append(payload[position + 1 : position + 1 + name_len].decode("latin1"))
                position += 1 + name_len
        elif etype == 43:  # supported_versions
            count = payload[0]
            versions = list(struct.unpack(f"!{count // 2}H", payload[1 : 1 + count]))
        elif etype == 51:  # key_share
            count = struct.unpack("!H", payload[0:2])[0]
            position = 2
            while position < 2 + count:
                group = struct.unpack("!H", payload[position : position + 2])[0]
                key_len = struct.unpack("!H", payload[position + 2 : position + 4])[0]
                key_share.append(group)
                position += 4 + key_len
        offset += 4 + elen
    stripped = lambda xs: sorted(x for x in xs if not is_grease(x))  # noqa: E731
    ja3_source = ",".join(
        (
            str(min(versions) if versions else 771),
            ",".join(map(str, stripped(ciphers))),
            ",".join(map(str, stripped(extensions))),
            ",".join(map(str, stripped(groups))),
            "0",
        )
    )
    out.update(
        {
            "tls_versions_offered": [hex(v) for v in versions] or [hex(0x0303)],
            "grease_in_ciphers": sum(1 for c in ciphers if is_grease(c)),
            "grease_in_extensions": [hex(e) for e in extensions if is_grease(e)],
            "cipher_count": len(ciphers),
            "extension_count": len(extensions),
            "extension_ids": stripped(extensions),
            "alpn": alpn,
            "sni": 0 in extensions,
            "supported_groups": stripped(groups),
            "key_share_groups": key_share,
            "signature_algorithm_count": len(sigalgs),
            "encrypted_client_hello": 0xFE0D in extensions or 65037 in extensions,
            "application_settings": 17513 in extensions or 17613 in extensions,
            "ja3": hashlib.md5(ja3_source.encode()).hexdigest(),
        }
    )
    return out


def make_cert(directory: Path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=10))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path, key_path = directory / "cert.pem", directory / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return str(certificate_path), str(key_path)


# The certificate has to exist before a client can verify it, so it lives in one temp directory
# for the life of the process rather than in the checkout.
_WORK = Path(tempfile.mkdtemp(prefix="websearch-transport-fp-"))

PAGE = (
    b"<html><head><title>fp</title></head><body><main><h1>Layer 3</h1><p>"
    + b"alpha beta gamma delta " * 40
    + b"</p></main></body></html>"
)


def h2_frame(frame_type: int, flags: int, stream: int, payload: bytes) -> bytes:
    """HTTP/2 frame header: 3-byte length, type, flags, then a 4-byte stream id."""
    return (
        struct.pack("!I", len(payload))[1:]
        + bytes((frame_type, flags))
        + struct.pack("!I", stream)
        + payload
    )


class Connection:
    """One accepted connection: ClientHello, handshake, then h2 frame capture."""

    def __init__(self, context: ssl.SSLContext, capture: dict):
        self.context = context
        self.capture = capture

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readexactly(5)
            record_len = struct.unpack("!H", head[3:5])[0]
            hello = head + await reader.readexactly(record_len)
            # A ClientHello larger than one record (e.g. with ECH) needs the remainder.
            if len(hello) >= 9 and hello[5] == 0x01:
                needed = 5 + 4 + struct.unpack("!I", b"\x00" + hello[6:9])[0]
                if len(hello) < needed:
                    hello += await reader.readexactly(needed - len(hello))
            self.capture["client_hello"] = parse_client_hello(hello)
        except (asyncio.IncompleteReadError, ssl.SSLError, struct.error, IndexError) as exc:
            self.capture["client_hello_error"] = repr(exc)
            writer.close()
            return

        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        bio = self.context.wrap_bio(incoming, outgoing, server_side=True)
        incoming.write(hello)
        try:
            while True:
                try:
                    bio.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    await self._flush(writer, outgoing)
                    chunk = await reader.read(65536)
                    if not chunk:
                        return
                    incoming.write(chunk)
            await self._flush(writer, outgoing)
            self.capture["negotiated_alpn"] = bio.selected_alpn_protocol()
            self.capture["negotiated_tls"] = bio.version()
            if bio.selected_alpn_protocol() == "h2":
                await self._capture_h2(reader, writer, bio, incoming, outgoing)
            else:
                head = (
                    b"HTTP/1.1 200 OK\r\ncontent-type: text/html; charset=utf-8\r\n"
                    + f"content-length: {len(PAGE)}\r\n\r\n".encode()
                    + PAGE
                )
                self._write_all(bio, head)
                await self._flush(writer, outgoing)
                await asyncio.sleep(0.5)
        except (ssl.SSLError, asyncio.IncompleteReadError, ConnectionResetError) as exc:
            self.capture["handshake_error"] = repr(exc)
        finally:
            writer.close()

    async def _flush(self, writer, outgoing) -> None:
        while True:
            chunk = outgoing.read()
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()

    @staticmethod
    def _parse_frames(buffer, frames, settings, pseudo, headers, decoder):
        """Log the frame sequence exactly as it arrived, before any library reorders it."""
        consumed, saw_headers, error = 0, False, None
        while len(buffer) - consumed >= 9:
            length = int.from_bytes(buffer[consumed : consumed + 3], "big")
            if len(buffer) - consumed < 9 + length:
                break
            ftype, flags = buffer[consumed + 3], buffer[consumed + 4]
            stream = struct.unpack("!I", buffer[consumed + 5 : consumed + 9])[0] & 0x7FFFFFFF
            payload = buffer[consumed + 9 : consumed + 9 + length]
            consumed += 9 + length
            label = FRAME_NAMES.get(ftype, f"TYPE{ftype}")
            try:
                if ftype == 0x4 and payload:  # SETTINGS
                    for index in range(0, len(payload), 6):
                        ident, value = struct.unpack("!HI", payload[index : index + 6])
                        settings.append(f"{SETTINGS_NAMES.get(ident, ident)}={value}")
                elif ftype == 0x8:  # WINDOW_UPDATE
                    label += f"+{struct.unpack('!I', payload)[0]}"
                elif ftype == 0x1:  # HEADERS
                    position, pad = 0, 0
                    if flags & 0x8:  # padded
                        pad = payload[0] + 1
                        position = pad
                    if flags & 0x20:  # priority
                        dependency = struct.unpack("!I", payload[position : position + 4])[0]
                        label += (
                            f"|prio(dep={dependency & 0x7FFFFFFF},"
                            f"{'excl' if dependency & 0x80000000 else 'plain'},"
                            f"w={payload[position + 4] + 1})"
                        )
                        position += 5
                    block = payload[position : len(payload) - pad if pad else len(payload)]
                    for name, _value in decoder.decode(block):
                        (pseudo if name.startswith(":") else headers).append(name)
                    saw_headers = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
            frames.append(f"{label}[flags={flags:#04x},stream={stream},len={length}]")
            if error:
                break
        return consumed, saw_headers, error

    @staticmethod
    def _write_all(bio, payload: bytes) -> None:
        """SSLObject.write may accept only part of the buffer."""
        view = memoryview(payload)
        while view:
            written = bio.write(view)
            view = view[written:]

    async def _capture_h2(self, reader, writer, bio, incoming, outgoing) -> None:
        debug = self.capture.setdefault("_debug", {"notes": []})
        # Send the server SETTINGS first: a browser holds its request HEADERS back until it has
        # seen them, so waiting for HEADERS before replying deadlocks the capture.
        self._write_all(bio, h2_frame(0x4, 0x0, 0, b""))
        await self._flush(writer, outgoing)
        buffer = b""
        preface_seen = False
        frames: list[str] = []
        settings: list[str] = []
        pseudo: list[str] = []
        headers: list[str] = []
        decoder = hpack.Decoder()
        got_headers = False
        while not got_headers:
            try:
                buffer += bio.read(65536)
            except ssl.SSLWantReadError:
                await self._flush(writer, outgoing)
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=8)
                except (TimeoutError, OSError) as exc:
                    debug["notes"].append(f"socket-{type(exc).__name__}")
                    break
                if not chunk:
                    debug["notes"].append("eof")
                    break
                incoming.write(chunk)
                continue
            except ssl.SSLZeroReturnError:
                debug["notes"].append("close-notify")
                break
            except Exception as exc:
                debug["notes"].append(f"read-raised {type(exc).__name__}: {exc!r}")
                break
            if not preface_seen:
                if len(buffer) < len(PREFACE):
                    continue
                self.capture["h2_preface_ok"] = buffer.startswith(PREFACE)
                buffer = buffer[len(PREFACE) :]
                preface_seen = True
            consumed, saw_headers, error = self._parse_frames(
                buffer, frames, settings, pseudo, headers, decoder
            )
            buffer = buffer[consumed:]
            if error:
                debug["notes"].append(f"parse-raised {error}")
                break
            got_headers = saw_headers
        self.capture["h2_frame_order"] = frames
        self.capture["h2_settings"] = settings
        self.capture["h2_pseudo_header_order"] = pseudo
        self.capture["h2_header_order"] = headers
        self.capture.pop("_debug", None)
        # Minimal response so the client finishes cleanly.
        try:
            encoded = hpack.Encoder().encode(
                [(":status", "200"), ("content-type", "text/html; charset=utf-8")]
            )
            self._write_all(bio, h2_frame(0x1, 0x5, 1, encoded))
            self._write_all(bio, h2_frame(0x0, 0x1, 1, PAGE))
            with contextlib.suppress(ssl.SSLError, ssl.SSLWantReadError):
                bio.unwrap()  # end the TLS layer so the client sees a clean close
            await self._flush(writer, outgoing)
            # Keep the connection open long enough for the client to consume the stream: closing
            # straight away reads as ERR_CONNECTION_CLOSED on the client side.
            await asyncio.sleep(1.5)
        except Exception as exc:
            self.capture["respond_error"] = repr(exc)


async def measure_real_http() -> None:
    """The HTTP reader must reach the capture server over TLS, so patch its URL validation."""
    directory = _WORK
    certificate, key = make_cert(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    context.set_alpn_protocols(["h2", "http/1.1"])
    captures: list[dict] = []

    async def entry(reader, writer):
        capture: dict = {}
        captures.append(capture)
        await Connection(context, capture).handle(reader, writer)

    server = await asyncio.start_server(entry, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        # Swap the trust anchor so the reader's own SSL context trusts this capture server;
        # everything else (http2, ALPN order, headers) is the production construction path.
        import certifi

        certifi.where = lambda: certificate  # type: ignore[assignment]
        from web_research.readers.http import HTTPReader

        reader = HTTPReader(allow_private_urls=True)
        try:
            document = await reader.read(f"https://127.0.0.1:{port}/report")
            print("HTTP reader:", document.status_code, document.method)
        except Exception as exc:
            captures.append({"client_error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        await asyncio.sleep(0.3)
        server.close()
        await server.wait_closed()
    print("===== HTTP reader (project class) =====")
    for capture in captures:
        if "client_hello" in capture or "client_error" in capture:
            print(json.dumps({k: v for k, v in capture.items() if k != "_debug"}, indent=1)[:1400])


async def measure(label: str, client) -> dict:
    directory = _WORK
    certificate, key = make_cert(directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    context.set_alpn_protocols(["h2", "http/1.1"])
    captures: list[dict] = []

    async def entry(reader, writer):
        capture: dict = {}
        captures.append(capture)
        await Connection(context, capture).handle(reader, writer)

    server = await asyncio.start_server(entry, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        try:
            await client(f"https://127.0.0.1:{port}/report")
        except Exception as exc:
            captures.append({"client_error": f"{type(exc).__name__}: {str(exc)[:300]}"})
        await asyncio.sleep(0.3)
        server.close()
        await server.wait_closed()

    print(f"\n===== {label} =====")
    for capture in captures:
        if "client_hello" in capture or "client_error" in capture:
            print(json.dumps(capture, indent=1, default=str))
    return captures


async def _httpx(url: str, http2: bool) -> None:
    import httpx

    async with httpx.AsyncClient(http2=http2, verify=str(_WORK / "cert.pem")) as client:
        response = await client.get(url)
        print("httpx status:", response.status_code, "protocol:", response.http_version)


async def _chromium(url: str, channel: str | None = None) -> None:
    from web_research.readers.crawl4ai import Crawl4AIReader

    class ProbeReader(Crawl4AIReader):
        def _browser_config(self, channel_value: str | None = None):
            config = super()._browser_config(channel_value)
            config.ignore_https_errors = True  # local capture server uses a throwaway certificate
            return config

    reader = ProbeReader(allow_private_urls=True, channel=channel or "")
    try:
        document = await reader.read(url)
        print("chromium read:", document.status_code, "chars:", len(document.content))
    finally:
        await reader.close()


def summarise(captures: list[dict]) -> dict:
    chosen = [c for c in captures if "client_hello" in c and c.get("negotiated_alpn")]
    capture = chosen[-1] if chosen else {}
    hello = capture.get("client_hello", {})
    return {
        "ja3": hello.get("ja3"),
        "grease_ciphers": hello.get("grease_in_ciphers"),
        "grease_extensions": hello.get("grease_in_extensions"),
        "cipher_count": hello.get("cipher_count"),
        "extension_count": hello.get("extension_count"),
        "extension_ids": hello.get("extension_ids"),
        "ech": hello.get("encrypted_client_hello"),
        "application_settings": hello.get("application_settings"),
        "alpn_offered": hello.get("alpn"),
        "alpn_used": capture.get("negotiated_alpn"),
        "tls": capture.get("negotiated_tls"),
        "groups": hello.get("supported_groups"),
        "sigalgs": hello.get("signature_algorithm_count"),
        "frames": capture.get("h2_frame_order"),
        "settings": capture.get("h2_settings"),
        "pseudo": capture.get("h2_pseudo_header_order"),
        "headers": capture.get("h2_header_order"),
        "error": capture.get("handshake_error") or capture.get("client_hello_error"),
    }


async def compare() -> None:
    for label, client in (
        ("HTTP reader today (httpx, http2=False)", lambda url: _httpx(url, False)),
        ("HTTP reader with h2 (httpx, http2=True)", lambda url: _httpx(url, True)),
        ("Browser reader (Chromium)", _chromium),
    ):
        captures = await measure_quietly(label, client)
        print(f"\n----- {label}")
        for line in json.dumps(summarise(captures), indent=1, default=str).splitlines():
            print("   ", line)


async def measure_quietly(label: str, client) -> list[dict]:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        captures = await measure(label, client)
    return captures


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "compare"
    if mode == "real-http":
        asyncio.run(measure_real_http())
    elif mode == "compare":
        asyncio.run(compare())
    elif mode == "httpx-h2":
        asyncio.run(measure("httpx (http2=True)", lambda url: _httpx(url, True)))
    elif mode == "httpx-h1":
        asyncio.run(measure("httpx (http2=False)", lambda url: _httpx(url, False)))
    elif mode == "chromium":
        asyncio.run(measure("Chromium via Crawl4AIReader", _chromium))
    elif mode == "chromium-channel":
        asyncio.run(measure("Chromium channel=chromium", lambda url: _chromium(url, "chromium")))
    else:
        raise SystemExit(f"unknown mode {mode}")


async def _real_http_reader(url: str) -> None:
    """Drive the project's own HTTPReader, which builds its own client and SSL context."""
    from web_research.readers.http import HTTPReader

    reader = HTTPReader(allow_private_urls=True)
    try:
        document = await reader.read(url.replace("https://127.0.0.1", "http://127.0.0.1"))
        print("HTTP reader:", document.status_code, document.method)
    finally:
        await reader.close()
