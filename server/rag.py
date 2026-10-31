"""本地知识库检索（轻量 RAG）。

设计取舍：
- **不引入向量数据库**：语料规模是"个人文档"，TF-IDF + 余弦相似度足够，且零外部服务；
- **纯 Python**：PDF 解析用 ``pypdf``（可选依赖，缺失时自动降级为只支持文本文件）；
- **只读、可控范围**：仅索引 ``knowledge.dirs``（默认等于 ``allowed_dirs``）下的白名单后缀文件，
  逐个 ``realpath`` 校验，阻断软链接/相对路径逃逸；
- **索引缓存**：内存中保存 chunk 向量，超过 ``cache_seconds`` 或显式 ``invalidate()`` 才重建。

对外接口：
- :meth:`KnowledgeBase.search` —— 返回 ``[{source, score, snippet, chunk_id}]``
- :meth:`KnowledgeBase.stats`  —— 索引状态（文件数、块数、跳过的文件）
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .config import Config

logger = logging.getLogger("edgeagent.rag")

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".log", ".csv", ".json", ".yml", ".yaml", ".ini", ".conf"}
PDF_SUFFIXES = {".pdf"}
MAX_PDF_PAGES = 300
SNIPPET_CHARS = 500

TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

#: 触发 RAG 的意图关键词（由 llm_client 调用，判断是否需要注入上下文）
KNOWLEDGE_HINTS: tuple[str, ...] = (
    "查找",
    "查找一下",
    "搜索",
    "检索",
    "总结",
    "概括",
    "归纳",
    "根据",
    "按照文档",
    "文档",
    "资料",
    "规范",
    "手册",
    "说明",
    "知识库",
    "pdf",
    "PDF",
    "报告",
    "条款",
)


def _import_pdf_reader() -> Any:
    """惰性导入 pypdf；未安装时返回 None。"""
    try:
        from pypdf import PdfReader  # type: ignore

        return PdfReader
    except Exception:  # pragma: no cover - 依赖缺失
        return None


@dataclass
class Chunk:
    """一个检索单元。"""

    source: str
    chunk_id: int
    text: str
    tf: dict[str, int] = field(default_factory=dict)
    norm: float = 0.0


@dataclass
class SearchHit:
    """检索结果。"""

    source: str
    score: float
    snippet: str
    chunk_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "score": round(self.score, 4),
            "snippet": self.snippet,
            "chunk_id": self.chunk_id,
        }


def tokenize(text: str) -> list[str]:
    """分词：英文/数字按单词，中文按二元组（bigram），足够做关键词检索。"""
    lowered = text.lower()
    tokens = TOKEN_RE.findall(lowered)
    for block in CJK_RE.findall(lowered):
        if len(block) == 1:
            tokens.append(block)
            continue
        tokens.extend(block[i : i + 2] for i in range(len(block) - 1))
    return tokens


def _split_chunks(text: str, size: int, overlap: int) -> list[str]:
    """按段落聚合成定长块，块间保留少量重叠，避免切断语义。"""
    normalized = re.sub(r"\r\n?", "\n", text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > size:  # 超长段落硬切
            if buffer:
                chunks.append(buffer)
                buffer = ""
            step = max(1, size - overlap)
            for start in range(0, len(para), step):
                chunks.append(para[start : start + size])
            continue
        if len(buffer) + len(para) + 1 <= size:
            buffer = f"{buffer}\n{para}".strip() if buffer else para
        else:
            chunks.append(buffer)
            tail = buffer[-overlap:] if overlap and buffer else ""
            buffer = f"{tail}\n{para}".strip() if tail else para
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if c.strip()]


class KnowledgeBase:
    """本地文档索引与检索。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._chunks: list[Chunk] = []
        self._idf: dict[str, float] = {}
        self._built_at: float = 0.0
        self._indexed_files = 0
        self._skipped: list[dict[str, str]] = []
        self._pdf_reader = _import_pdf_reader()

    # ---------------- 对外接口 ----------------
    @property
    def enabled(self) -> bool:
        return self._cfg.knowledge.enabled

    def invalidate(self) -> None:
        """强制下次检索时重建索引。"""
        with self._lock:
            self._built_at = 0.0

    def stats(self) -> dict[str, Any]:
        """索引状态（前端/审计用）。"""
        self._ensure_index()
        return {
            "enabled": self.enabled,
            "dirs": list(self._cfg.knowledge.dirs),
            "indexed_files": self._indexed_files,
            "chunks": len(self._chunks),
            "vocabulary": len(self._idf),
            "pdf_supported": self._pdf_reader is not None,
            "built_at": datetime.fromtimestamp(self._built_at).isoformat(timespec="seconds") if self._built_at else "",
            "skipped": self._skipped[:10],
        }

    def search(self, query: str, top_k: int | None = None) -> list[SearchHit]:
        """TF-IDF 余弦相似度检索。查询为空或无命中时返回空列表。"""
        if not self.enabled or not query or not query.strip():
            return []
        self._ensure_index()
        if not self._chunks:
            return []

        limit = max(1, min(20, top_k or self._cfg.knowledge.top_k))
        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        query_tf: dict[str, int] = {}
        for token in query_tokens:
            query_tf[token] = query_tf.get(token, 0) + 1

        query_vec: dict[str, float] = {}
        for token, count in query_tf.items():
            idf = self._idf.get(token)
            if idf:
                query_vec[token] = count * idf
        if not query_vec:
            return []
        query_norm = math.sqrt(sum(v * v for v in query_vec.values())) or 1.0

        scored: list[SearchHit] = []
        for chunk in self._chunks:
            if chunk.norm <= 0:
                continue
            dot = 0.0
            for token, weight in query_vec.items():
                tf = chunk.tf.get(token)
                if tf:
                    dot += weight * (tf * self._idf.get(token, 0.0))
            if dot <= 0:
                continue
            score = dot / (chunk.norm * query_norm)
            if score > 0:
                scored.append(
                    SearchHit(
                        source=chunk.source,
                        score=score,
                        snippet=chunk.text[:SNIPPET_CHARS],
                        chunk_id=chunk.chunk_id,
                    )
                )

        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:limit]

    # ---------------- 索引构建 ----------------
    def _ensure_index(self) -> None:
        ttl = self._cfg.knowledge.cache_seconds
        if self._chunks and (time.time() - self._built_at) < ttl:
            return
        with self._lock:
            if self._chunks and (time.time() - self._built_at) < ttl:
                return
            self._build_index()

    def _build_index(self) -> None:
        started = time.time()
        chunks: list[Chunk] = []
        skipped: list[dict[str, str]] = []
        indexed_files = 0

        for path in self._iter_files(skipped):
            try:
                text = self._extract_text(path)
            except Exception as exc:  # 单个文件失败不影响整体
                skipped.append({"file": path, "reason": f"解析失败: {exc}"})
                continue
            if not text.strip():
                skipped.append({"file": path, "reason": "无可用文本"})
                continue
            pieces = _split_chunks(text, self._cfg.knowledge.chunk_chars, self._cfg.knowledge.chunk_overlap)
            if not pieces:
                continue
            indexed_files += 1
            for index, piece in enumerate(pieces):
                tf: dict[str, int] = {}
                for token in tokenize(piece):
                    tf[token] = tf.get(token, 0) + 1
                chunks.append(Chunk(source=path, chunk_id=index, text=piece, tf=tf))

        # 计算 IDF 与向量模长
        doc_freq: dict[str, int] = {}
        for chunk in chunks:
            for token in chunk.tf:
                doc_freq[token] = doc_freq.get(token, 0) + 1
        total = max(1, len(chunks))
        idf = {token: math.log((total + 1) / (freq + 1)) + 1.0 for token, freq in doc_freq.items()}

        for chunk in chunks:
            norm = math.sqrt(sum((count * idf.get(token, 0.0)) ** 2 for token, count in chunk.tf.items()))
            chunk.norm = norm

        self._chunks = chunks
        self._idf = idf
        self._indexed_files = indexed_files
        self._skipped = skipped
        self._built_at = time.time()
        logger.info(
            "知识库索引完成：%s 个文件 / %s 块 / 词表 %s（%.0fms）",
            indexed_files,
            len(chunks),
            len(idf),
            (self._built_at - started) * 1000,
        )

    def _iter_files(self, skipped: list[dict[str, str]]) -> Iterable[str]:
        """遍历配置目录下的可读文件（realpath 校验、大小与数量限制）。"""
        limit_bytes = int(self._cfg.knowledge.max_file_mb * 1024 * 1024)
        roots = [os.path.realpath(d) for d in self._cfg.knowledge.dirs if d]
        seen: set[str] = set()
        count = 0

        for root in roots:
            if not os.path.isdir(root):
                skipped.append({"file": root, "reason": "目录不存在"})
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if count >= self._cfg.knowledge.max_files:
                        skipped.append({"file": "*", "reason": f"超过 max_files={self._cfg.knowledge.max_files}"})
                        return
                    path = os.path.realpath(os.path.join(dirpath, name))
                    # 软链接可能指向目录外，必须再次校验归属
                    if not any(path == r or path.startswith(r + os.sep) for r in roots):
                        skipped.append({"file": path, "reason": "超出知识库目录"})
                        continue
                    suffix = os.path.splitext(name)[1].lower()
                    if suffix not in TEXT_SUFFIXES and suffix not in PDF_SUFFIXES:
                        continue
                    if suffix in PDF_SUFFIXES and self._pdf_reader is None:
                        skipped.append({"file": path, "reason": "未安装 pypdf，跳过 PDF"})
                        continue
                    try:
                        if os.path.getsize(path) > limit_bytes:
                            skipped.append({"file": path, "reason": "文件超过大小上限"})
                            continue
                    except OSError:
                        continue
                    if path in seen:
                        continue
                    seen.add(path)
                    count += 1
                    yield path

    def _extract_text(self, path: str) -> str:
        suffix = os.path.splitext(path)[1].lower()
        if suffix in PDF_SUFFIXES:
            return self._extract_pdf(path)
        return self._read_text_file(path)

    @staticmethod
    def _read_text_file(path: str) -> str:
        for encoding in ("utf-8-sig", "utf-8", "gbk", "utf-16", "latin-1"):
            try:
                with open(path, "r", encoding=encoding) as handle:
                    return handle.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        return ""

    def _extract_pdf(self, path: str) -> str:
        if self._pdf_reader is None:
            return ""
        reader = self._pdf_reader(path)
        parts: list[str] = []
        for index, page in enumerate(reader.pages):
            if index >= MAX_PDF_PAGES:
                break
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # 单页失败跳过
                continue
        return "\n".join(parts)


def needs_knowledge(text: str) -> bool:
    """判断用户这句话是否可能需要查本地文档（避免每轮都做检索）。"""
    if not text:
        return False
    return any(hint in text for hint in KNOWLEDGE_HINTS)
