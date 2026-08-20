"""
知识库自动归档模块
每半个月将高重要度未关联知识库的记忆打包写入知识库。
由定时任务触发，可交给GLM执行。
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional

from astrbot.api import logger

DB_PATH = "/AstrBot/data/memory_manager.db"
KB_NAME_PREFIX = "沈砚清自动归档"
EMBEDDING_PROVIDER = "Qwen/Qwen3-Embedding-8B"
IMPORTANCE_THRESHOLD = 7
MAX_DOCS_PER_KB = 80  # 每个知识库最多放80个文档，满了开新的


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_unlinked_memories(min_importance: int = IMPORTANCE_THRESHOLD) -> List[dict]:
    """获取高重要度且未关联知识库的记忆"""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, category, content, tags, importance, layer, aliases
            FROM memories
            WHERE (kb_doc_id IS NULL OR kb_doc_id = '')
              AND importance >= ?
              AND status = 'active'
              AND fact_status = 'current'
            ORDER BY created_at ASC
            """,
            (min_importance,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def group_memories_by_half_month(memories: List[dict]) -> dict:
    """按半月分组：1-15日一组，16-月底一组"""
    groups = {}
    for m in memories:
        created = m["created_at"][:10]  # YYYY-MM-DD
        try:
            dt = datetime.strptime(created, "%Y-%m-%d")
        except ValueError:
            continue
        if dt.day <= 15:
            key = f"{dt.year}-{dt.month:02d}-上"
        else:
            key = f"{dt.year}-{dt.month:02d}-下"
        if key not in groups:
            groups[key] = []
        groups[key].append(m)
    return groups


def format_memories_as_chunks(memories: List[dict]) -> List[str]:
    """将一组记忆格式化为知识库文档chunks"""
    chunks = []
    for m in memories:
        lines = []
        lines.append(f"## 记忆#{m['id']} [{m['category']}] 重要度{m['importance']}")
        lines.append(f"时间：{m['created_at'][:16]}")
        if m.get("tags"):
            lines.append(f"标签：{m['tags']}")
        if m.get("aliases"):
            lines.append(f"别名：{m['aliases']}")
        lines.append(f"层级：{m.get('layer', 'event')}")
        lines.append("")
        lines.append(m["content"])
        chunks.append("\n".join(lines))
    return chunks


def update_kb_doc_ids(memory_ids: List[int], doc_id: str) -> None:
    """回写kb_doc_id到记忆表"""
    conn = _get_connection()
    try:
        placeholders = ",".join("?" * len(memory_ids))
        conn.execute(
            f"UPDATE memories SET kb_doc_id = ? WHERE id IN ({placeholders})",
            [doc_id] + memory_ids,
        )
        conn.commit()
    finally:
        conn.close()


async def find_or_create_archive_kb(kb_manager) -> "KBHelper":
    """找到当前归档知识库，满了就创建新的"""
    existing_kbs = []
    all_kb_ids = list(kb_manager.kb_insts.keys())
    
    for kb_id, kb_helper in kb_manager.kb_insts.items():
        if kb_helper.kb.kb_name.startswith(KB_NAME_PREFIX):
            existing_kbs.append(kb_helper)
    
    if not existing_kbs:
        # 没有归档知识库，创建第一个
        kb_helper = await kb_manager.create_kb(
            kb_name=f"{KB_NAME_PREFIX}（一）",
            description="记忆系统自动归档",
            emoji="📦",
            embedding_provider_id=EMBEDDING_PROVIDER,
            chunk_size=512,
            chunk_overlap=50,
        )
        logger.info(f"创建归档知识库：{kb_helper.kb.kb_name}")
        return kb_helper
    
    # 找最新的那个（编号最大的）
    existing_kbs.sort(key=lambda h: h.kb.kb_name)
    latest = existing_kbs[-1]
    
    # 检查文档数量
    docs = await latest.list_documents()
    if len(docs) >= MAX_DOCS_PER_KB:
        # 满了，开新的
        num_map = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十",
                   "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十"]
        next_num = len(existing_kbs)
        if next_num < len(num_map):
            name = f"{KB_NAME_PREFIX}（{num_map[next_num]}）"
        else:
            name = f"{KB_NAME_PREFIX}（{next_num + 1}）"
        
        kb_helper = await kb_manager.create_kb(
            kb_name=name,
            description="记忆系统自动归档",
            emoji="📦",
            embedding_provider_id=EMBEDDING_PROVIDER,
            chunk_size=512,
            chunk_overlap=50,
        )
        logger.info(f"归档知识库已满，创建新的：{name}")
        return kb_helper
    
    return latest


async def run_archive(kb_manager) -> str:
    """执行一次归档，返回结果摘要"""
    memories = get_unlinked_memories()
    
    if not memories:
        return "没有需要归档的记忆。"
    
    groups = group_memories_by_half_month(memories)
    
    results = []
    total_archived = 0
    
    for period, mems in sorted(groups.items()):
        if len(mems) < 3:
            # 不足3条的暂不归档，等攒够
            results.append(f"{period}: {len(mems)}条，暂不归档（不足3条）")
            continue
        
        # 获取或创建归档知识库
        kb_helper = await find_or_create_archive_kb(kb_manager)
        
        # 格式化为chunks
        chunks = format_memories_as_chunks(mems)
        
        # 上传文档
        file_name = f"记忆归档_{period}.md"
        try:
            doc = await kb_helper.upload_document(
                file_name=file_name,
                file_content=None,
                file_type="md",
                pre_chunked_text=chunks,
            )
            
            # 回写kb_doc_id
            memory_ids = [m["id"] for m in mems]
            update_kb_doc_ids(memory_ids, doc.doc_id)
            
            total_archived += len(mems)
            results.append(f"{period}: {len(mems)}条 → {kb_helper.kb.kb_name}/{file_name}")
            logger.info(f"归档完成：{period}, {len(mems)}条记忆 → {doc.doc_id}")
            
        except Exception as e:
            results.append(f"{period}: 归档失败 - {e}")
            logger.error(f"归档失败：{period} - {e}")
    
    summary = f"归档完成。共{total_archived}条记忆写入知识库。\n" + "\n".join(results)
    return summary
