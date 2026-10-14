"""Loopback-only browser interface for the Fluke 8845A acquisition worker."""

from __future__ import annotations

import argparse
import json
import mimetypes
import signal
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from PySide6.QtCore import QCoreApplication, QStandardPaths

from . import __version__
from .modes import DESCRIPTORS
from .webservice import WebRuntime

_WEB_ROOT = Path(__file__).with_name("web")
_MAX_COMMAND_BYTES = 32 * 1024
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    block_on_close = False

    def __init__(self, address, handler, runtime: WebRuntime):
        self.runtime = runtime
        super().__init__(address, handler)


class LocalWebHandler(BaseHTTPRequestHandler):
    server: LocalWebServer
    protocol_version = "HTTP/1.1"
    server_version = "Fluke8845A-local"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:
        # Keep the terminal useful during experiments without logging query/body contents.
        print(f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {self.client_address[0]} {format % args}", file=sys.stderr)

    def _expected_host(self) -> str:
        port = self.server.server_port
        return "127.0.0.1" if port == 80 else f"127.0.0.1:{port}"

    def _check_local_request(self, *, require_origin: bool = False) -> bool:
        if self.client_address[0] != "127.0.0.1":
            self._send_json(403, {"error": "仅允许本机浏览器访问"})
            return False
        if self.headers.get("Host", "") != self._expected_host():
            self._send_json(403, {"error": "无效的本地 Host 请求"})
            return False
        if require_origin:
            expected = f"http://{self._expected_host()}"
            if self.headers.get("Origin", "") != expected:
                self._send_json(403, {"error": "命令请求必须来自本地界面"})
                return False
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site and fetch_site != "same-origin":
                self._send_json(403, {"error": "已拒绝跨站命令"})
                return False
        return True

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _not_found(self) -> None:
        self._send_json(404, {"error": "未找到该页面"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._check_local_request():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/health":
            self._send_json(200, {"status": "ok", "version": __version__, "bind": "127.0.0.1"})
            return
        if parsed.path == "/api/meta":
            modes = []
            for mode, spec in DESCRIPTORS.items():
                modes.append({
                    "value": mode.value,
                    "label": spec.label,
                    "quantity": spec.quantity,
                    "unit": spec.unit_si,
                    "ranges": list(spec.ranges_si),
                    "nplc": list(spec.nplc_values),
                    "bandwidths": list(spec.ac_bandwidths_hz),
                    "analog_filter": spec.analog_filter,
                    "safety": spec.safety,
                })
            documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            if not documents:
                documents = str(Path.home() / "Documents")
            self._send_json(200, {
                "version": __version__, "modes": modes, "server": "loopback",
                "default_root": str(Path(documents) / "Fluke8845A_Experiments"),
            })
            return
        if parsed.path == "/api/ports":
            try:
                from serial.tools import list_ports
                ports = [{"device": port.device, "description": port.description} for port in list_ports.comports()]
                self._send_json(200, {"ports": ports})
            except Exception as exc:
                self._send_json(200, {"ports": [], "error": str(exc)})
            return
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query)
            try:
                since_sample = max(0, int(query.get("since_sample", ["0"])[0]))
                since_log = max(0, int(query.get("since_log", ["0"])[0]))
                generation_value = query.get("generation", [None])[0]
                generation = None if generation_value is None else int(generation_value)
            except ValueError:
                self._send_json(400, {"error": "状态游标必须是整数"})
                return
            self._send_json(200, self.server.runtime.store.snapshot(
                since_sample=since_sample, since_log=since_log, generation=generation,
            ))
            return
        if parsed.path == "/api/latest.csv":
            self._send_latest_csv()
            return
        if parsed.path.startswith("/api/"):
            self._not_found()
            return
        if parsed.path == "/":
            path = _WEB_ROOT / "index.html"
        elif parsed.path in ("/app.css", "/app.js"):
            path = _WEB_ROOT / parsed.path.lstrip("/")
        else:
            self._not_found()
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(500, {"error": "本地界面资源不可读取"})
            return
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        charset = "; charset=utf-8" if mime.startswith(("text/", "application/javascript")) else ""
        self._send_bytes(200, body, mime + charset)

    def _send_latest_csv(self) -> None:
        directory = self.server.runtime.store.snapshot().get("last_experiment_dir", "")
        if not directory:
            self._send_json(404, {"error": "当前还没有实验 CSV"})
            return
        path = (Path(directory) / "measurements.csv").resolve()
        if path.name != "measurements.csv" or not path.is_file():
            self._send_json(404, {"error": "实验 CSV 不可读取"})
            return
        try:
            size = path.stat().st_size
            filename = quote(f"{Path(directory).name}_measurements.csv")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
            for key, value in _SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            with path.open("rb") as stream:
                remaining = size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            # Headers may already be sent; dropping this connection is safer than inventing data.
            self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._check_local_request(require_origin=True):
            return
        if urlsplit(self.path).path != "/api/command":
            self._not_found()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "无效的请求长度"})
            return
        if not 1 <= length <= _MAX_COMMAND_BYTES:
            self._send_json(413 if length > _MAX_COMMAND_BYTES else 400, {"error": "命令请求大小无效"})
            return
        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "命令必须是 UTF-8 JSON"})
            return
        if not isinstance(request, dict) or not isinstance(request.get("action"), str):
            self._send_json(400, {"error": "命令格式无效"})
            return
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "命令参数必须是对象"})
            return
        result = self.server.runtime.broker.request(request["action"], payload)
        self._send_json(200 if result.get("success") else 400, result)

    def do_HEAD(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "不支持 HEAD 请求"})


def create_server(runtime: WebRuntime, port: int = 8765) -> LocalWebServer:
    """Create an IPv4 loopback server. Binding to other interfaces is unsupported."""
    return LocalWebServer(("127.0.0.1", int(port)), LocalWebHandler, runtime)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 Fluke 8845A localhost 浏览器界面")
    parser.add_argument("--port", type=int, default=8765, help="本机端口（默认 8765；使用 0 分配临时端口）")
    parser.add_argument("--no-browser", action="store_true", help="启动服务但不自动打开默认浏览器")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口号必须在 0 至 65535 之间")

    QCoreApplication.setApplicationName("Fluke8845A Local Web")
    app = QCoreApplication(sys.argv[:1])
    runtime = WebRuntime()
    runtime.start()
    try:
        server = create_server(runtime, args.port)
    except OSError as exc:
        runtime.stop()
        print(f"无法启动 localhost 服务：{exc}", file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{server.server_port}/"
    serve_thread = threading.Thread(target=server.serve_forever, name="8845A-local-web", daemon=True)
    serve_thread.start()
    runtime.router.shutdown_requested.connect(
        lambda: threading.Timer(0.7, app.quit).start()
    )
    print(f"Fluke 8845A localhost 界面已启动：{url}")
    print("可按 Ctrl+C 或在页面选择“退出服务”安全关闭；仪器只由此进程中的单个采集线程访问。")

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_args: app.quit())
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url, new=2)).start()
    try:
        app.exec()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        server.shutdown()
        server.server_close()
        runtime.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
