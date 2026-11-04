"""HuggingFace（经镜像）访问封装。

只做四件事：搜索模型、读取详情、列出文件、列出分支。
所有请求都走可配置的服务地址（默认 hf-mirror.com），可选代理。

注意：这里用 transport（背后是 curl）而不是 requests，
原因是本机环境下 Python 的 TLS 握手会被对端重置，只有 curl 能连通。
"""

from __future__ import annotations

import threading
import time
from urllib.parse import quote

import transport

DEFAULT_ENDPOINT = "https://hf-mirror.com"

SORTS = {
    "downloads": "下载量最多",
    "likes": "点赞最多",
    "lastModified": "最近更新",
    "trendingScore": "最近热门",
}

_lock = threading.RLock()
_cache: dict[tuple, tuple[float, object]] = {}
_CACHE_TTL = 60.0

settings: dict = {"endpoint": DEFAULT_ENDPOINT, "proxy": ""}


def configure(cfg: dict) -> None:
    with _lock:
        settings["endpoint"] = (cfg.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")
        settings["proxy"] = (cfg.get("proxy") or "").strip()
        _cache.clear()


def endpoint() -> str:
    return settings.get("endpoint") or DEFAULT_ENDPOINT


def proxy() -> str:
    return (settings.get("proxy") or "").strip()


def _get_json(url: str, params: dict | None = None, timeout: int = 45):
    return transport.get_json(url, params, timeout=timeout, proxy=proxy())


def _cached(key: tuple, producer):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = producer()
    _cache[key] = (now, value)
    if len(_cache) > 400:
        for k in list(_cache)[:200]:
            _cache.pop(k, None)
    return value


def explain_error(exc: BaseException) -> str:
    """把底层异常翻译成用户能看懂的一句话。"""
    text = str(exc)
    low = text.lower()

    if "http 404" in low:
        return "模型或文件不存在，检查模型 ID 是否写对（形如 Qwen/Qwen2.5-7B-Instruct）"
    if "http 401" in low or "http 403" in low:
        return "这个模型需要授权（gated 或私有），本工具暂不支持登录下载"
    if "http 429" in low:
        return "请求太频繁被限流了，等一会儿再试"
    if "http 5" in low:
        return "服务端出错（5xx），通常是镜像或 HuggingFace 那边的问题，稍后再试"
    if "resolve host" in low or "could not resolve" in low:
        return f"域名解析失败：{endpoint()}，检查地址拼写或网络"
    if "connection refused" in low or "connection reset" in low:
        return f"连接被拒绝或重置（{endpoint()}），换服务地址或填代理试试"
    if "timed out" in low or "timeout" in low:
        return "连接超时，检查网络，或在设置里换服务地址 / 填代理"
    if "ssl" in low or "tls" in low:
        return "TLS 握手失败，这个地址可能被网络拦截了，换服务地址试试"
    if "curl 失败" in text:
        return f"网络请求失败：{text[:160]}"
    if "不是 json" in text:
        return "这个地址返回的不是模型数据，可能不是 HuggingFace 镜像"
    return f"{type(exc).__name__}: {text[:200]}"


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------
def search_models(query: str, page: int = 1, page_size: int = 12, sort: str = "downloads") -> dict:
    query = (query or "").strip()
    sort = sort if sort in SORTS else "downloads"
    page = max(1, int(page))
    key = ("search", query, page, page_size, sort, endpoint())

    def produce():
        params = {"limit": page * page_size, "sort": sort, "direction": -1}
        if query:
            params["search"] = query
        rows = _get_json(f"{endpoint()}/api/models", params)
        if not isinstance(rows, list):
            rows = []
        chunk = rows[(page - 1) * page_size:]
        items = []
        for data in chunk:
            repo_id = data.get("id") or data.get("modelId") or ""
            if not repo_id:
                continue
            tags = data.get("tags") or []
            pipe = data.get("pipeline_tag")
            items.append({
                "repo_id": repo_id,
                "display_name": repo_id.split("/")[-1],
                "downloads": data.get("downloads") or 0,
                "likes": data.get("likes") or 0,
                "tasks": [pipe] if pipe else [],
                "license": next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), ""),
                "description": "",
                "file_size": 0,
                "last_modified": data.get("lastModified"),
            })
        return {"items": items, "page": page, "page_size": page_size,
                "has_more": len(rows) >= page * page_size}

    try:
        return _cached(key, produce)
    except Exception as exc:  # noqa: BLE001
        return {"items": [], "page": page, "page_size": page_size, "has_more": False,
                "error": explain_error(exc)}


# ---------------------------------------------------------------------------
# 详情 / 文件列表 / 分支
# ---------------------------------------------------------------------------
def model_detail(repo_id: str) -> dict:
    repo_id = normalize_repo_id(repo_id)
    if not repo_id:
        return {"error": "请输入模型 ID，形如 Qwen/Qwen2.5-7B-Instruct"}

    def produce():
        data = _get_json(f"{endpoint()}/api/models/{repo_id}")
        tags = data.get("tags") or []
        return {
            "repo_id": repo_id,
            "display_name": repo_id.split("/")[-1],
            "description": "",
            "downloads": data.get("downloads") or 0,
            "likes": data.get("likes") or 0,
            "license": next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), ""),
            "tags": tags[:20],
            "tasks": [data.get("pipeline_tag")] if data.get("pipeline_tag") else [],
            "file_size": 0,
            "gated": bool(data.get("gated")),
        }

    try:
        return _cached(("detail", repo_id, endpoint()), produce)
    except Exception as exc:  # noqa: BLE001
        return {"error": explain_error(exc)}


def list_files(repo_id: str, revision: str = "main") -> dict:
    repo_id = normalize_repo_id(repo_id)
    revision = (revision or "main").strip()

    def produce():
        # blobs=true 才会带上每个文件的大小（含 LFS 大文件）
        data = _get_json(f"{endpoint()}/api/models/{repo_id}",
                         {"blobs": "true", "revision": revision})
        files = []
        total = 0
        for item in data.get("siblings") or []:
            path = item.get("rfilename") or ""
            if not path or path.endswith("/"):
                continue
            size = int(item.get("size") or 0)
            files.append({"path": path, "size": size})
            total += size
        files.sort(key=lambda f: f["path"])
        return {"repo_id": repo_id, "revision": revision, "files": files,
                "total_files": len(files), "total_size": total}

    try:
        return _cached(("files", repo_id, revision, endpoint()), produce)
    except Exception as exc:  # noqa: BLE001
        return {"error": explain_error(exc), "files": [], "total_files": 0, "total_size": 0}


def list_revisions(repo_id: str) -> dict:
    repo_id = normalize_repo_id(repo_id)

    def produce():
        data = _get_json(f"{endpoint()}/api/models/{repo_id}/refs")
        if not isinstance(data, dict):
            return ["main"]
        branches = [b.get("name") for b in (data.get("branches") or []) if b.get("name")]
        tags = [t.get("name") for t in (data.get("tags") or []) if t.get("name")]
        ordered = ["main"] + [b for b in branches if b != "main"] + tags[:20]
        seen, out = set(), []
        for name in ordered:
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out or ["main"]

    try:
        return {"revisions": _cached(("refs", repo_id, endpoint()), produce)}
    except Exception as exc:  # noqa: BLE001
        return {"revisions": ["main"], "error": explain_error(exc)}


def file_url(repo_id: str, revision: str, path: str) -> str:
    return f"{endpoint()}/{repo_id}/resolve/{quote(revision or 'main')}/{quote(path)}"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def normalize_repo_id(text: str) -> str:
    """接受模型 ID、HuggingFace / 镜像的模型页链接，统一成 owner/name。"""
    text = (text or "").strip().strip('"').strip("'")
    if not text:
        return ""
    lowered = text.lower()
    for marker in ("/models/", "/model/"):
        if marker in lowered:
            text = text[lowered.index(marker) + len(marker):]
            break
    text = text.split("?")[0].split("#")[0].replace("\\", "/").strip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def looks_like_repo_id(text: str) -> bool:
    text = (text or "").strip()
    if "huggingface.co" in text.lower() or "hf-mirror" in text.lower():
        return True
    return "/" in text and len(text.split("/")) == 2 and " " not in text
