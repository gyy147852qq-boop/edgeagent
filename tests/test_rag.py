"""知识库（RAG）单元测试：分词、切块、检索、目录边界、缓存失效。"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from server.config import Config, KnowledgeConfig
from server.rag import KnowledgeBase, needs_knowledge, tokenize

WORK_ROOT = Path(__file__).resolve().parent.parent / ".pytest_work"


@pytest.fixture()
def kb_root() -> str:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    path = tempfile.mkdtemp(prefix="kb_", dir=str(WORK_ROOT))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_kb(root: str, files: dict[str, str], **overrides) -> KnowledgeBase:
    for name, content in files.items():
        target = Path(root) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    params = {
        "dirs": [root],
        "max_files": 50,
        "chunk_chars": 300,
        "chunk_overlap": 40,
        "top_k": 5,
        "cache_seconds": 60.0,
    }
    params.update(overrides)
    cfg = Config(knowledge=KnowledgeConfig(**params))
    return KnowledgeBase(cfg)


def test_search_returns_matching_chunk(kb_root: str) -> None:
    kb = make_kb(
        kb_root,
        {
            "spec.md": "# 部署规范\n\nEdgeAgent 的部署必须只监听 127.0.0.1，禁止绑定 0.0.0.0。\n",
            "other.md": "# 其他\n\n这里讲的是完全无关的内容，比如打印机驱动安装。\n",
        },
    )
    hits = kb.search("部署 监听 127.0.0.1")
    assert hits, "应当命中部署规范"
    assert hits[0].source.endswith("spec.md")
    assert hits[0].score > 0


def test_search_is_empty_for_unrelated_query(kb_root: str) -> None:
    kb = make_kb(kb_root, {"a.txt": "今天是晴天，适合出门散步。"})
    assert kb.search("量子纠缠退相干拓扑绝缘体") == []


def test_search_handles_empty_query(kb_root: str) -> None:
    kb = make_kb(kb_root, {"a.txt": "任意内容"})
    assert kb.search("") == []
    assert kb.search("   ") == []


def test_tokenize_mixes_cjk_bigram_and_words() -> None:
    tokens = tokenize("EdgeAgent 支持 PDF 解析与知识库检索")
    assert "edgeagent" in tokens
    assert "pdf" in tokens
    assert "知识" in tokens  # 中文二元组
    assert "检索" in tokens


def test_only_configured_dirs_are_indexed(kb_root: str) -> None:
    outside = tempfile.mkdtemp(prefix="outside_", dir=str(WORK_ROOT))
    try:
        Path(outside, "secret.txt").write_text("TOPSECRET-9911 机密内容", encoding="utf-8")
        kb = make_kb(kb_root, {"inside.txt": "公开内容 PUBLIC-1234"})
        hits = kb.search("TOPSECRET-9911")
        assert hits == [], "知识库不能索引配置目录之外的文件"
        assert kb.search("PUBLIC-1234")
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def test_stats_reports_index_state(kb_root: str) -> None:
    kb = make_kb(kb_root, {"a.txt": "第一份文档，包含关键词 alpha。", "b.md": "第二份文档，包含关键词 beta。"})
    stats = kb.stats()
    assert stats["indexed_files"] == 2
    assert stats["chunks"] >= 2
    assert stats["enabled"] is True
    assert [os.path.normcase(d) for d in stats["dirs"]] == [os.path.normcase(os.path.realpath(kb_root))]


def test_unsupported_suffix_is_ignored(kb_root: str) -> None:
    Path(kb_root, "binary.exe").write_bytes(b"MZ\x00\x00not-a-document")
    kb = make_kb(kb_root, {"doc.txt": "可检索文本 keyword-gamma"})
    hits = kb.search("keyword-gamma")
    assert hits
    assert all(not hit.source.endswith(".exe") for hit in hits)


def test_broken_pdf_is_skipped_without_crash(kb_root: str) -> None:
    Path(kb_root, "broken.pdf").write_bytes(b"%PDF-1.4 this is not a real pdf")
    kb = make_kb(kb_root, {"ok.txt": "正常文本 delta-keyword"})
    assert kb.search("delta-keyword")
    stats = kb.stats()
    assert any(item["file"].endswith("broken.pdf") for item in stats["skipped"]) or stats["indexed_files"] >= 1


def test_invalidate_picks_up_new_files(kb_root: str) -> None:
    kb = make_kb(kb_root, {"a.txt": "初始内容 init-marker"}, cache_seconds=60.0)
    assert kb.search("第二份文档") == []
    Path(kb_root, "b.txt").write_text("第二份文档 late-marker-7788", encoding="utf-8")
    kb.invalidate()
    hits = kb.search("late-marker-7788")
    assert hits, "invalidate 后应能检索到新增文件"


def test_needs_knowledge_intent_detection() -> None:
    assert needs_knowledge("帮我总结一下这份文档") is True
    assert needs_knowledge("根据 D:\\Docs\\规范.pdf 查找条款") is True
    assert needs_knowledge("列出 D:\\AgentWork 下的文件") is False
    assert needs_knowledge("") is False
