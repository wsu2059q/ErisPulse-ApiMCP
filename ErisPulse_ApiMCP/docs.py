"""ErisPulse 框架文档检索（ApiMCP 自有实现，不依赖任何其它生态模块）。

文档来源是框架仓库 docs/ 目录（按版本 tag 发布）：
1. 启动/每日检查：本地缓存版本 vs 已安装框架版本，不一致则按 tag 下载 zip 解压
2. 本地目录源（docs_source 指向框架仓库 docs/）开发环境零网络
3. 离线兜底：复用 epsdk-mcp 的 ~/.cache/erispulse-mcp/docs 缓存（若存在）
4. 检索：markdown 按标题切块 + bigram 词面打分
"""
from __future__ import annotations

import asyncio
import io
import re
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

META_KEY = "ApiMCP:docs_meta"
DEFAULT_DOCS_DIR = "data/ApiMCP/docs"
GITHUB_ZIP = "https://codeload.github.com/ErisPulse/ErisPulse/zip/refs/tags/{tag}"
MCP_CACHE = Path.home() / ".cache" / "erispulse-mcp" / "docs"
CHUNK_CHARS = 1200
MAX_FILES = 400
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_WORD_RE = re.compile(r"[\s\W_]+")


def framework_version() -> str:
    """已安装框架版本：dist metadata 优先，包属性兜底。"""
    try:
        from importlib.metadata import version as _version

        return str(_version("ErisPulse") or "")
    except Exception:
        pass
    try:
        import ErisPulse as _ep

        return str(getattr(_ep, "__version__", "") or "")
    except Exception:
        return ""


def _bigrams(text: str) -> set:
    cleaned = _WORD_RE.sub("", str(text or "").lower())
    if len(cleaned) < 2:
        return {cleaned} if cleaned else set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _overlap(query: str, content: str) -> float:
    q = _bigrams(query)
    if not q:
        return 0.0
    return len(q & _bigrams(content)) / len(q)


def _chunk_markdown(text: str, doc_title: str) -> List[Dict[str, str]]:
    """按标题切块；超长块再按空行切到 CHUNK_CHARS 以内。"""
    chunks: List[Dict[str, str]] = []
    heading = doc_title
    buffer: List[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        buffer.clear()
        if not body:
            return
        if len(body) <= CHUNK_CHARS:
            chunks.append({"title": heading or doc_title, "content": body})
            return
        part: List[str] = []
        size = 0
        for para in re.split(r"\n{2,}", body):
            if size and size + len(para) > CHUNK_CHARS:
                chunks.append({"title": heading or doc_title, "content": "\n\n".join(part)})
                part, size = [], 0
            part.append(para)
            size += len(para) + 2
        if part:
            chunks.append({"title": heading or doc_title, "content": "\n\n".join(part)})

    for line in text.splitlines():
        match = HEADING_RE.match(line.strip())
        if match:
            flush()
            heading = match.group(2).strip() or doc_title
            continue
        buffer.append(line)
    flush()
    return chunks


class DocsEngine:
    """文档缓存 + 索引 + 检索（async 同步，sync 检索）。"""

    def __init__(self, module: Any):
        """
        :param module: 宿主 Main 实例（提供 sdk / logger / _conf()）
        """
        self.module = module
        self.sdk = module.sdk
        self.logger = module.logger
        self._index: Optional[List[Dict[str, str]]] = None
        self._index_version = ""
        self._lock = asyncio.Lock()

    # ==================== 配置与路径 ====================

    def _conf(self) -> Any:
        return self.module._conf()

    def docs_dir(self) -> Path:
        raw = str(getattr(self._conf(), "docs_dir", "") or DEFAULT_DOCS_DIR)
        return Path(raw).expanduser()

    def lang(self) -> str:
        return str(getattr(self._conf(), "docs_lang", "zh-CN") or "zh-CN")

    def _meta(self) -> Dict[str, Any]:
        try:
            data = self.sdk.storage.get(META_KEY, {})
        except Exception:
            data = {}
        return dict(data) if isinstance(data, dict) else {}

    def _set_meta(self, **updates: Any) -> None:
        meta = self._meta()
        meta.update(updates)
        try:
            self.sdk.storage.set(META_KEY, meta)
        except Exception as e:
            self.logger.debug(f"docs meta persist failed: {e}")

    def cached_version(self) -> str:
        return str(self._meta().get("version") or "")

    # ==================== 根目录解析 ====================

    def _local_source_root(self) -> Optional[Path]:
        raw = str(getattr(self._conf(), "docs_source", "") or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_dir():
            return None
        return self._lang_root(path)

    def _lang_root(self, base: Path) -> Path:
        if not base.is_dir():
            return base
        preferred = base / self.lang()
        if preferred.is_dir() and any(preferred.glob("**/*.md")):
            return preferred
        if any(base.glob("*.md")):
            return base
        for candidate in sorted(p for p in base.iterdir() if p.is_dir()):
            if any(candidate.glob("**/*.md")):
                return candidate
        return base

    def _cache_root(self, version: str) -> Path:
        return self.docs_dir() / (version or "latest") / self.lang()

    def _mcp_cache_root(self) -> Optional[Path]:
        if not MCP_CACHE.is_dir():
            return None
        return self._lang_root(MCP_CACHE)

    def active_root(self) -> Optional[Path]:
        """当前生效的文档根目录（本地源 > 版本缓存 > 其它工具缓存）。"""
        local = self._local_source_root()
        if local is not None:
            return local
        cached = self._cache_root(self.cached_version())
        if cached.is_dir() and any(cached.glob("**/*.md")):
            return cached
        return self._mcp_cache_root()

    # ==================== 同步 ====================

    async def ensure_synced(self, force: bool = False) -> Dict[str, Any]:
        """文档与已安装框架版本对齐；网络失败沿用本地缓存，不阻塞启动。"""
        if self._local_source_root() is not None:
            self._set_meta(version=f"local:{framework_version() or 'dev'}", source="local", synced_at=time.time())
            self._invalidate()
            return {"enabled": True, "source": "local", "root": str(self._local_source_root())}
        version = framework_version() or "latest"
        meta = self._meta()
        last_check = float(meta.get("checked_at") or 0)
        auto = bool(getattr(self._conf(), "docs_auto_sync", True))
        if not force:
            if self.cached_version() == version and self.active_root() is not None:
                return {"enabled": True, "source": "cache", "version": version, "cached": True}
            if not auto or (last_check and time.time() - last_check < 86400):
                root = self.active_root()
                return {"enabled": True, "source": "stale", "version": self.cached_version(),
                        "root": str(root) if root else ""}
        async with self._lock:
            if not force and self.cached_version() == version and self.active_root() is not None:
                return {"enabled": True, "source": "cache", "version": version, "cached": True}
            result = await self._download(version)
            self._set_meta(checked_at=time.time())
            return result

    async def _download(self, version: str) -> Dict[str, Any]:
        custom = str(getattr(self._conf(), "docs_source", "") or "").strip()
        if custom.startswith(("http://", "https://")):
            urls = [custom]
        else:
            tags = [f"v{version}"] if version and version != "latest" else []
            tags.append(version)
            urls = [GITHUB_ZIP.format(tag=tag) for tag in tags]
        last_error = ""
        for url in urls:
            try:
                data = await self._fetch_bytes(url)
                if not data:
                    continue
                written = self._extract(data, version)
                if written:
                    self._set_meta(version=version, source=url, synced_at=time.time())
                    self._invalidate()
                    self.logger.info(f"docs synced for framework {version}: {written} files")
                    return {"enabled": True, "source": "github", "version": version,
                            "root": str(self._cache_root(version)), "files": written}
            except Exception as e:
                last_error = str(e)
                self.logger.debug(f"docs download failed ({url}): {e}")
        root = self.active_root()
        if last_error:
            self.logger.warning(f"docs sync failed for {version}: {last_error}")
        return {"enabled": True, "source": "stale", "version": self.cached_version(),
                "root": str(root) if root else "", "error": last_error}

    async def _fetch_bytes(self, url: str) -> bytes:
        timeout = float(getattr(self._conf(), "docs_timeout", 30) or 30)
        client = getattr(self.sdk, "client", None)
        if client is not None and hasattr(client, "get"):
            resp = await client.get(url, timeout=timeout)
            status = getattr(resp, "status", 200)
            if status != 200:
                raise RuntimeError(f"HTTP {status}")
            read = getattr(resp, "read", None)
            if callable(read):
                return await read()
            content = getattr(resp, "content", None)
            if content is not None:
                return await content.read() if hasattr(content, "read") else bytes(content)

        import urllib.request

        def _blocking() -> bytes:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.read()

        return await asyncio.to_thread(_blocking)

    def _extract(self, data: bytes, version: str) -> int:
        """从 GitHub zip 中只取 docs/<lang>/ 下的 markdown。"""
        target = self._cache_root(version)
        target.mkdir(parents=True, exist_ok=True)
        lang = self.lang()
        written = 0
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.lower().endswith(".md"):
                    continue
                parts = info.filename.split("/")
                if "docs" not in parts:
                    continue
                rest = parts[parts.index("docs") + 1:]
                if not rest:
                    continue
                if len(rest) >= 2 and rest[0] == lang:
                    relative = Path(*rest[1:])
                elif len(rest) >= 2 and rest[0] != lang:
                    continue  # 其它语言目录跳过
                else:
                    relative = Path(*rest)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                dest = (target / relative).resolve()
                if not str(dest).startswith(str(target.resolve())):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(archive.read(info))
                written += 1
                if written >= MAX_FILES:
                    break
        return written

    # ==================== 索引与检索 ====================

    def _invalidate(self) -> None:
        self._index = None
        self._index_version = ""

    def _build_index(self) -> List[Dict[str, str]]:
        version = self.cached_version()
        if self._index is not None and self._index_version == version:
            return self._index
        index: List[Dict[str, str]] = []
        root = self.active_root()
        if root is None:
            self._index, self._index_version = index, version
            return index
        count = 0
        for path in sorted(root.glob("**/*.md")):
            if count >= MAX_FILES:
                break
            count += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            relative = path.relative_to(root).as_posix()
            title = Path(relative).stem
            for chunk in _chunk_markdown(text, title):
                index.append({"file": relative, "title": chunk["title"], "content": chunk["content"]})
        self._index, self._index_version = index, version
        return index

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            return []
        scored = []
        for chunk in self._build_index():
            score = _overlap(query, chunk["title"]) * 1.5 + _overlap(query, chunk["content"])
            if score <= 0:
                continue
            scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {"file": chunk["file"], "title": chunk["title"],
             "snippet": chunk["content"][:600], "score": round(score, 3)}
            for score, chunk in scored[: max(1, int(top_k or 5))]
        ]

    def read(self, file: str, limit: int = 8000) -> str:
        root = self.active_root()
        if root is None:
            return "docs not ready: run refresh first (no cached docs and no local source)"
        try:
            resolved = (root / str(file or "")).resolve()
            if not str(resolved).startswith(str(root.resolve())):
                return f"path escapes docs root: {file}"
        except Exception as e:
            return f"read failed: {e}"
        if not resolved.is_file():
            return f"doc not found: {file}"
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"read failed: {e}"
        return text[: max(500, int(limit or 8000))]

    def status(self) -> Dict[str, Any]:
        root = self.active_root()
        return {
            "version": self.cached_version(),
            "component_version": framework_version() or "unknown",
            "root": str(root) if root else "",
            "files": len(list(root.glob("**/*.md"))) if root else 0,
            "lang": self.lang(),
        }
