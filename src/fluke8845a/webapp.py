"""Loopback-only browser interface for the Fluke 8845A acquisition worker."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import re
import signal
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from PySide6.QtCore import QCoreApplication, QStandardPaths

from . import __version__
from .filter_policy import DIGITAL_FILTER_INFO
from .modes import DESCRIPTORS
from .path_security import resolve_safe_child_file
from .share_export import export_share_package
from .storage import (
    complete_csv_prefix_size,
    write_excel_safe_events_csv,
    write_excel_safe_measurements_csv,
)
from .webservice import WebRuntime

_WEB_ROOT = Path(__file__).with_name("web")
_MAX_COMMAND_BYTES = 32 * 1024
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def _complete_csv_prefix_size(path: Path, chunk_size: int = 64 * 1024) -> int:
    """Return the byte length through the last complete CSV record boundary."""
    return complete_csv_prefix_size(path, chunk_size=chunk_size)


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
        stream = getattr(sys, "stderr", None)
        if stream is None:
            return
        try:
            print(
                f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                f"{self.client_address[0]} {format % args}",
                file=stream,
            )
        except (OSError, ValueError):
            # Frozen GUI executables can have missing or closed console streams.
            # Request logging must not prevent an HTTP response from being sent.
            return

    def _expected_host(self) -> str:
        port = self.server.server_port
        return "127.0.0.1" if port == 80 else f"127.0.0.1:{port}"

    def _check_local_request(self, *, require_origin: bool = False) -> bool:
        if self.client_address[0] != "127.0.0.1":
            self._send_json(403, {"error": "仅允许本机浏览器访问"}, close=True)
            return False
        if self.headers.get("Host", "") != self._expected_host():
            self._send_json(403, {"error": "无效的本地 Host 请求"}, close=True)
            return False
        if require_origin:
            expected = f"http://{self._expected_host()}"
            if self.headers.get("Origin", "") != expected:
                self._send_json(403, {"error": "命令请求必须来自本地界面"}, close=True)
                return False
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site and fetch_site != "same-origin":
                self._send_json(403, {"error": "已拒绝跨站命令"}, close=True)
                return False
        return True

    def _send_bytes(self, status: int, body: bytes, content_type: str, *, close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: dict, *, close: bool = False) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", close=close)

    def _not_found(self) -> None:
        self._send_json(404, {"error": "未找到该页面"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        if not self._check_local_request():
            return
        if parsed.path == "/api/command-status":
            expected_origin = f"http://{self._expected_host()}"
            origin = self.headers.get("Origin", "")
            fetch_site = self.headers.get("Sec-Fetch-Site", "")
            if (origin and origin != expected_origin) or (fetch_site and fetch_site != "same-origin"):
                self._send_json(403, {"error": "命令状态查询必须来自本地界面"})
                return
            if not origin and fetch_site != "same-origin":
                self._send_json(403, {"error": "命令状态查询缺少同源浏览器标记"})
                return
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
                    "wiring_group": spec.wiring_group,
                    "rms": spec.rms,
                    "ranges": list(spec.ranges_si),
                    "nplc": list(spec.nplc_values),
                    "bandwidths": list(spec.ac_bandwidths_hz),
                    "analog_filter": spec.analog_filter,
                    "digital_filter": spec.digital_filter,
                    "input_impedance_auto": spec.input_impedance_auto,
                    "safety": spec.safety,
                    "safety_limits": spec.safety_limits,
                })
            documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            if not documents:
                documents = str(Path.home() / "Documents")
            self._send_json(200, {
                "version": __version__, "modes": modes, "server": "loopback",
                "digital_filter_info": DIGITAL_FILTER_INFO,
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
        if parsed.path == "/api/command-status":
            operation_id = parse_qs(parsed.query).get("id", [""])[0]
            if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                self._send_json(400, {"error": "命令状态编号无效"})
                return
            status = self.server.runtime.broker.operation_status(operation_id)
            if status is None:
                self._send_json(404, {"error": "该命令状态已过期或不存在；请先核对仪器状态"})
                return
            self._send_json(200, status)
            return
        if parsed.path == "/api/chart/overview":
            query = parse_qs(parsed.query)
            status, payload = self.server.runtime.chart_history.overview(query.get("width", [1200])[0])
            self._send_json(status, payload)
            return
        if parsed.path == "/api/chart/range/cancel":
            query = parse_qs(parsed.query)
            status, payload = self.server.runtime.chart_history.cancel_range(query.get("request_id", [""])[0])
            self._send_json(status, payload)
            return
        if parsed.path == "/api/chart/range":
            query = parse_qs(parsed.query)
            request_id = query.get("request_id", [""])[0]
            if all(key in query for key in ("job_id", "start", "end", "width")):
                try:
                    start_s = float(query["start"][0])
                    end_s = float(query["end"][0])
                    width = int(query["width"][0])
                    if not math.isfinite(start_s) or not math.isfinite(end_s):
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    self._send_json(400, {"error": "图表范围参数必须是有限数值和整数宽度"})
                    return
                status, payload = self.server.runtime.chart_history.start_range(
                    query["job_id"][0], request_id, start_s, end_s, width,
                )
            else:
                status, payload = self.server.runtime.chart_history.range_status(request_id)
            self._send_json(status, payload)
            return
        if parsed.path == "/api/latest.csv":
            self._send_latest_csv()
            return
        if parsed.path == "/api/latest-events-safe.csv":
            self._send_latest_events_safe_csv()
            return
        if parsed.path == "/api/latest-measurements-safe.csv":
            self._send_latest_measurements_safe_csv()
            return
        if parsed.path == "/api/latest-share-bundle.zip":
            self._send_latest_share_bundle()
            return
        if parsed.path.startswith("/api/"):
            self._not_found()
            return
        if parsed.path == "/":
            path = _WEB_ROOT / "index.html"
        elif parsed.path in ("/app.css", "/app.js", "/history_worker.js"):
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
        state = self.server.runtime.store.snapshot()
        active_snapshot = bool(state.get("experiment_dir"))
        directory = state.get("experiment_dir") or state.get("last_experiment_dir", "")
        if not directory:
            self._send_json(404, {"error": "当前还没有实验 CSV"})
            return
        path = resolve_safe_child_file(directory, "measurements.csv")
        if path is None:
            self._send_json(404, {"error": "实验 CSV 不可读取"})
            return
        try:
            snapshot_time = datetime.now().astimezone().isoformat(timespec="seconds")
            size = _complete_csv_prefix_size(path) if active_snapshot else path.stat().st_size
            if size <= 0:
                self._send_json(404, {"error": "实验 CSV 中还没有可导出的完整记录"})
                return
            marker = "进行中快照" if active_snapshot else "已保存"
            filename = quote(f"{Path(directory).name}_{marker}_{snapshot_time.replace(':', '-')}_measurements.csv")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
            self.send_header("X-8845A-Export-State", "in-progress-snapshot" if active_snapshot else "saved")
            self.send_header("X-8845A-Snapshot-Time", snapshot_time)
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

    def _send_latest_events_safe_csv(self) -> None:
        state = self.server.runtime.store.snapshot()
        if state.get("experiment_dir"):
            self._send_json(409, {"error": "请停止采集后再导出事件安全副本；原始 events.csv 始终保留不变"})
            return
        directory = state.get("last_experiment_dir", "")
        if not directory:
            self._send_json(404, {"error": "当前还没有已完成实验的事件 CSV"})
            return
        path = resolve_safe_child_file(directory, "events.csv")
        if path is None:
            self._send_json(404, {"error": "实验事件 CSV 不可读取"})
            return
        try:
            with tempfile.TemporaryDirectory(prefix="fluke8845a-events-safe-") as temporary_directory:
                safe_path = Path(temporary_directory) / "events_excel_safe.csv"
                write_excel_safe_events_csv(path, safe_path)
                size = safe_path.stat().st_size
                filename = quote(f"{Path(directory).name}_events_excel_safe.csv")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    with safe_path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            self.wfile.write(chunk)
                except OSError:
                    # Once headers are sent, dropping the connection is safer than inventing data.
                    self.close_connection = True
        except (OSError, ValueError) as exc:
            self._send_json(400, {"error": f"无法生成事件安全副本：{exc}"})

    def _send_latest_measurements_safe_csv(self) -> None:
        state = self.server.runtime.store.snapshot()
        if state.get("experiment_dir"):
            self._send_json(409, {"error": "请停止采集后再导出测量安全副本；原始 measurements.csv 始终保留不变"})
            return
        directory = state.get("last_experiment_dir", "")
        if not directory:
            self._send_json(404, {"error": "当前还没有已完成实验的测量 CSV"})
            return
        path = resolve_safe_child_file(directory, "measurements.csv")
        if path is None:
            self._send_json(404, {"error": "实验测量 CSV 不可读取"})
            return
        try:
            with tempfile.TemporaryDirectory(prefix="fluke8845a-measurements-safe-") as temporary_directory:
                safe_path = Path(temporary_directory) / "measurements_excel_safe.csv"
                write_excel_safe_measurements_csv(path, safe_path)
                size = safe_path.stat().st_size
                filename = quote(f"{Path(directory).name}_measurements_excel_safe.csv")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    with safe_path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            self.wfile.write(chunk)
                except OSError:
                    self.close_connection = True
        except (OSError, ValueError) as exc:
            self._send_json(400, {"error": f"无法生成测量安全副本：{exc}"})

    def _send_latest_share_bundle(self) -> None:
        state = self.server.runtime.store.snapshot()
        if state.get("experiment_dir"):
            self._send_json(409, {"error": "请停止采集并结束实验后，再导出脱敏分享包。原始实验文件不会更改。"})
            return
        directory = state.get("last_experiment_dir", "")
        if not directory:
            self._send_json(404, {"error": "当前没有可导出的已完成实验。"})
            return
        try:
            with tempfile.TemporaryDirectory(prefix="fluke8845a-share-") as temporary_directory:
                archive_path = Path(temporary_directory) / "Fluke8845A_脱敏分享包.zip"
                export_share_package(directory, archive_path)
                size = archive_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header(
                    "Content-Disposition",
                    "attachment; filename=Fluke8845A_share_bundle.zip; "
                    f"filename*=UTF-8''{quote('Fluke8845A_脱敏分享包.zip', safe='')}",
                )
                for key, value in _SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    with archive_path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            self.wfile.write(chunk)
                except OSError:
                    # Once headers are sent, dropping the connection is safer than inventing data.
                    self.close_connection = True
        except Exception:
            # Helper errors can contain source paths. Keep them out of the HTTP response.
            self._send_json(
                400,
                {"error": "无法生成脱敏分享包。请确认已完成实验包含有效的 config.json 和 measurements.csv；events.csv 可选。"},
            )

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
        response_status = 202 if result.get("pending") else (200 if result.get("success") else 400)
        self._send_json(response_status, result)

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
        while not runtime.stop():
            print("localhost 服务线程仍在收尾；等待采集线程安全关闭。", file=sys.stderr, flush=True)
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
        while not runtime.stop():
            print("采集线程仍在完成当前操作；等待串口与数据文件安全关闭。", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
