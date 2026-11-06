"""HuggingFace 模型下载器 —— 本地网页服务。

用 Python 自带的 HTTP 服务实现，不依赖 Web 框架；
只监听 127.0.0.1，界面用浏览器打开。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    BASE = Path(sys.executable).resolve().parent
    BUNDLE = Path(getattr(sys, "_MEIPASS", BASE))
else:
    BASE = Path(__file__).resolve().parent.parent
    BUNDLE = BASE

APP_DIR = BUNDLE / "app"
WEB_DIR = APP_DIR / "web"
DATA_DIR = BASE / "data"
LOG_DIR = DATA_DIR / "logs"

if not FROZEN:
    sys.path.insert(0, str(APP_DIR))

import hf_client  # noqa: E402
import transport  # noqa: E402
from download_manager import DownloadManager  # noqa: E402

DEFAULT_SETTINGS = {
    "download_dir": str(BASE / "models"),
    "max_parallel_models": 2,
    "files_per_model": 3,
    "port": 8766,
    "endpoint": hf_client.DEFAULT_ENDPOINT,
    "proxy": "off",
    "revision_default": "main",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}

_started_at = time.time()


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    path = DATA_DIR / "settings.json"
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                for key in DEFAULT_SETTINGS:
                    if key in stored:
                        settings[key] = stored[key]
        except Exception:
            pass
    return settings


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "settings.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_DIR / "server.log", "a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


SETTINGS = load_settings()
hf_client.configure(SETTINGS)
MANAGER = DownloadManager(DATA_DIR, SETTINGS)


def versions() -> dict:
    out = {"python": "%d.%d.%d" % sys.version_info[:3]}
    try:
        import requests

        out["requests"] = requests.__version__
    except Exception:
        out["requests"] = "未知"
    out["endpoint"] = hf_client.endpoint()
    out["proxy"] = hf_client.proxy() or "直连（未用代理）"
    return out


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "HFStudio/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _query(self) -> dict:
        parsed = parse_qs(urlparse(self.path).query)
        return {k: v[0] for k, v in parsed.items()}

    def _serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._json({"ok": False, "error": "文件不存在"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix.lower(),
                                                           "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    # -- 路由 ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = self._query()
        try:
            if route in ("/", "/index.html"):
                self._serve_file(WEB_DIR / "index.html")
            elif route.startswith("/static/"):
                rel = unquote(route[len("/static/"):])
                target = (WEB_DIR / rel).resolve()
                root = WEB_DIR.resolve()
                if target == root or root in target.parents:
                    self._serve_file(target)
                else:
                    self._json({"ok": False, "error": "非法路径"}, 403)
            elif route == "/favicon.ico":
                self._serve_file(WEB_DIR / "favicon.svg")
            elif route == "/api/ping":
                self._json({"ok": True, "app": "hf-studio", "pid": os.getpid()})
            elif route == "/api/state":
                state = MANAGER.state()
                state["ok"] = True
                state["settings"] = SETTINGS
                state["library"] = MANAGER.library()
                state["uptime"] = int(time.time() - _started_at)
                state["versions"] = versions()
                self._json(state)
            elif route == "/api/search":
                result = hf_client.search_models(
                    query.get("q", ""),
                    page=int(query.get("page", 1) or 1),
                    page_size=min(40, int(query.get("size", 12) or 12)),
                    sort=query.get("sort", "downloads"),
                )
                result["ok"] = "error" not in result
                self._json(result)
            elif route == "/api/model":
                result = hf_client.model_detail(query.get("repo_id", ""))
                result["ok"] = "error" not in result
                self._json(result)
            elif route == "/api/files":
                result = hf_client.list_files(query.get("repo_id", ""),
                                              query.get("revision", "main"))
                result["ok"] = "error" not in result
                self._json(result)
            elif route == "/api/revisions":
                result = hf_client.list_revisions(query.get("repo_id", ""))
                result["ok"] = True
                self._json(result)
            elif route == "/api/library":
                result = MANAGER.library(force=query.get("force") == "1")
                result["ok"] = True
                self._json(result)
            elif route == "/api/settings":
                self._json({"ok": True, "settings": SETTINGS, "defaults": DEFAULT_SETTINGS})
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as exc:  # noqa: BLE001
            log("GET " + route + " 失败: " + traceback.format_exc())
            self._json({"ok": False, "error": hf_client.explain_error(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        body = self._read_json()
        try:
            if route == "/api/tasks":
                task = MANAGER.add_task(
                    body.get("repo_id") or "",
                    body.get("revision") or SETTINGS.get("revision_default") or "main",
                    body.get("files") or None,
                    body.get("display_name") or "",
                )
                MANAGER.save_now()
                self._json({"ok": True, "task": task})
            elif route.startswith("/api/tasks/"):
                parts = [p for p in route.split("/") if p]
                if len(parts) == 4:
                    task_id, action = parts[2], parts[3]
                    if action == "remove":
                        MANAGER.remove(task_id, delete_files=False)
                        self._json({"ok": True})
                        return
                    handler = {"pause": MANAGER.pause, "resume": MANAGER.resume,
                               "cancel": MANAGER.cancel, "retry": MANAGER.retry}.get(action)
                    if handler is None:
                        self._json({"ok": False, "error": "未知操作"}, 400)
                        return
                    self._json({"ok": True, "task": handler(task_id)})
                else:
                    self._json({"ok": False, "error": "未知接口"}, 404)
            elif route == "/api/settings":
                patch = body or {}
                if "download_dir" in patch:
                    target = Path(str(patch["download_dir"])).expanduser()
                    try:
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        self._json({"ok": False, "error": f"这个目录不可用：{exc}"}, 400)
                        return
                    SETTINGS["download_dir"] = str(target)
                if "max_parallel_models" in patch:
                    SETTINGS["max_parallel_models"] = max(1, min(6, int(patch["max_parallel_models"])))
                if "files_per_model" in patch:
                    SETTINGS["files_per_model"] = max(1, min(16, int(patch["files_per_model"])))
                if "endpoint" in patch:
                    value = str(patch["endpoint"]).strip().rstrip("/")
                    if not value.startswith(("http://", "https://")):
                        self._json({"ok": False, "error": "服务地址要以 http:// 或 https:// 开头"}, 400)
                        return
                    SETTINGS["endpoint"] = value
                if "proxy" in patch:
                    SETTINGS["proxy"] = str(patch["proxy"]).strip()
                if "revision_default" in patch:
                    SETTINGS["revision_default"] = str(patch["revision_default"]) or "main"
                save_settings(SETTINGS)
                hf_client.configure(SETTINGS)
                MANAGER.settings = SETTINGS
                self._json({"ok": True, "settings": SETTINGS})
            elif route == "/api/test-endpoint":
                self._json(test_endpoint(body.get("endpoint") or SETTINGS["endpoint"],
                                         body.get("proxy", SETTINGS.get("proxy"))))
            elif route == "/api/open":
                target = Path(str(body.get("path") or "")).expanduser()
                if not target.exists():
                    self._json({"ok": False, "error": "这个路径不存在，可能已经被删除了"}, 400)
                    return
                os.startfile(str(target))  # noqa: S606
                self._json({"ok": True})
            elif route == "/api/open-log":
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                os.startfile(str(LOG_DIR))  # noqa: S606
                self._json({"ok": True})
            elif route == "/api/delete-folder":
                import shutil

                target = Path(str(body.get("path") or "")).expanduser()
                root = Path(SETTINGS["download_dir"]).resolve()
                try:
                    resolved = target.resolve()
                except OSError:
                    self._json({"ok": False, "error": "路径无法解析"}, 400)
                    return
                if not resolved.exists():
                    self._json({"ok": False, "error": "文件夹已经不在了"}, 400)
                    return
                if resolved == root or root not in resolved.parents:
                    self._json({"ok": False, "error": "只能删除下载目录里面的文件夹"}, 400)
                    return
                shutil.rmtree(resolved, ignore_errors=True)
                MANAGER._library_cache = None
                self._json({"ok": True})
            elif route == "/api/browse":
                picked = pick_directory(str(body.get("initial") or SETTINGS["download_dir"]))
                self._json({"ok": True, "path": picked})
            elif route == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except KeyError as exc:
            self._json({"ok": False, "error": str(exc)}, 404)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            log("POST " + route + " 失败: " + traceback.format_exc())
            self._json({"ok": False, "error": hf_client.explain_error(exc)}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path
        query = self._query()
        try:
            if route.startswith("/api/tasks/"):
                MANAGER.remove(route.split("/")[-1], delete_files=query.get("delete_files") == "1")
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "未知接口"}, 404)
        except KeyError as exc:
            self._json({"ok": False, "error": str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            log("DELETE 失败: " + traceback.format_exc())
            self._json({"ok": False, "error": hf_client.explain_error(exc)}, 500)


# ---------------------------------------------------------------------------
# 服务地址连通性测试
# ---------------------------------------------------------------------------
def test_endpoint(url: str, proxy: str = "") -> dict:
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "服务地址要以 http:// 或 https:// 开头"}
    ok, cost, message = transport.ping(url, proxy)
    if ok:
        return {"ok": True, "latency": cost, "message": message}
    # ping 返回的已经是面向用户的说明，不要再套一层「模型不存在」之类的翻译
    return {"ok": False, "error": message}


# ---------------------------------------------------------------------------
# 选择文件夹
# ---------------------------------------------------------------------------
BROWSE_SCRIPT = r"""
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
initial = sys.argv[1] if len(sys.argv) > 1 else "."
path = filedialog.askdirectory(initialdir=initial, title="选择模型保存位置", mustexist=False)
sys.stdout.write(path or "")
"""


def pick_directory(initial: str) -> str:
    if FROZEN:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                path = filedialog.askdirectory(initialdir=initial or ".",
                                               title="选择模型保存位置", mustexist=False)
            finally:
                root.destroy()
            return path or ""
        except Exception as exc:  # noqa: BLE001
            log("选择目录失败: " + repr(exc))
            return ""
    creation = 0x08000000 if os.name == "nt" else 0
    try:
        proc = subprocess.run([sys.executable, "-c", BROWSE_SCRIPT, initial],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=600, creationflags=creation)
        return (proc.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        log("选择目录失败: " + repr(exc))
        return ""


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def create_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def reachable(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def main() -> int:
    open_browser = "--open" in sys.argv
    preferred = int(SETTINGS.get("port") or 8766)
    server = None
    port = preferred
    for candidate in range(preferred, preferred + 20):
        if reachable(candidate):
            port = candidate
            if open_browser:
                webbrowser.open(f"http://127.0.0.1:{port}")
            print(f"已经在运行：http://127.0.0.1:{port}")
            return 2
        try:
            server = create_server(candidate)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        print("端口被占用，无法启动。")
        return 1

    url = f"http://127.0.0.1:{port}"
    print("=" * 58)
    print("  HuggingFace 模型下载器")
    print(f"  界面地址: {url}")
    print(f"  服务地址: {hf_client.endpoint()}")
    print(f"  模型保存: {SETTINGS['download_dir']}")
    print("  关闭这个窗口即停止服务")
    print("=" * 58)
    log(f"服务启动 {url}")

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.save_now()
        server.server_close()
        log("服务退出")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
