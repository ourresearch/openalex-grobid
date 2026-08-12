"""A real HTTP server that impersonates GROBID, for integration tests.

The point of a real server rather than a mocked `requests.post` is that the
things most likely to break are in the plumbing, not the branching: whether the
multipart body is rebuilt correctly on a retry, whether a read timeout actually
fires on the socket, whether a connection error is raised where we expect it.
A stub that returns canned response objects cannot fail those ways.

Usage:

    with FakeGrobid([{"status": 503}, {"status": 200, "body": TEI}]) as server:
        ...  # server.url points at it, server.requests records what arrived
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time


class ReceivedRequest:
    """What the server actually received, decoded from the multipart body."""

    def __init__(self, path, fields, file_name, file_bytes):
        self.path = path
        self.fields = fields          # form fields, e.g. {"segmentSentences": "1"}
        self.file_name = file_name    # filename from the "input" part
        self.file_bytes = file_bytes  # the PDF bytes as they arrived

    def __repr__(self):
        return (f"<ReceivedRequest {self.path} fields={self.fields} "
                f"file={self.file_name} {len(self.file_bytes)}B>")


def parse_multipart(body, content_type):
    """Minimal multipart/form-data parser.

    stdlib `cgi` is gone in 3.13 and `email` mangles binary payloads, so this
    splits on the boundary directly. Good enough for the shape requests sends.
    """
    boundary = None
    for chunk in content_type.split(";"):
        chunk = chunk.strip()
        if chunk.startswith("boundary="):
            boundary = chunk[len("boundary="):].strip('"')
    if boundary is None:
        raise ValueError(f"no boundary in content-type: {content_type!r}")

    delimiter = b"--" + boundary.encode()
    fields, file_name, file_bytes = {}, None, None

    for part in body.split(delimiter):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        raw_headers, _, payload = part.partition(b"\r\n\r\n")
        headers = raw_headers.decode("utf-8", "replace")

        name = _header_param(headers, "name")
        filename = _header_param(headers, "filename")
        if filename is not None:
            file_name, file_bytes = filename, payload
        elif name is not None:
            fields[name] = payload.decode("utf-8", "replace")

    return fields, file_name, file_bytes


def _header_param(headers, key):
    marker = f'{key}="'
    start = headers.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = headers.find('"', start)
    return headers[start:end]


class FakeGrobid:
    """Scriptable stand-in for the GROBID sidecar.

    `script` is a list of directives applied to successive requests; the last
    one repeats once the list is exhausted (so `[{"status": 503}]` means "always
    busy"). Supported keys:

        status  int    HTTP status to return (default 200)
        body    bytes  response body (default b"")
        delay   float  seconds to stall before responding — stalls past the
                       client's read timeout to produce a real ReadTimeout
        drop    bool   close the connection without replying at all
    """

    def __init__(self, script=None):
        self.script = list(script or [{"status": 200, "body": b"<TEI/>"}])
        self.requests = []
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    @property
    def url(self):
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def request_count(self):
        with self._lock:
            return len(self.requests)

    def _next_directive(self):
        with self._lock:
            index = len(self.requests) - 1
        if index >= len(self.script):
            index = len(self.script) - 1
        return self.script[index]

    def _record(self, received):
        with self._lock:
            self.requests.append(received)

    def start(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def _make_handler(fake):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # keep pytest output readable

        def do_GET(self):
            fake._record(ReceivedRequest(self.path, {}, None, b""))
            self._respond(fake._next_directive())

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            fields, file_name, file_bytes = parse_multipart(
                body, self.headers.get("Content-Type", "")
            )
            fake._record(
                ReceivedRequest(self.path, fields, file_name, file_bytes or b"")
            )
            self._respond(fake._next_directive())

        def _respond(self, directive):
            if directive.get("delay"):
                time.sleep(directive["delay"])
            if directive.get("drop"):
                self.close_connection = True
                return
            body = directive.get("body", b"")
            try:
                self.send_response(directive.get("status", 200))
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # the client hit its timeout and walked away; that is the
                # scenario under test, not a failure of the server
                pass

    return Handler
