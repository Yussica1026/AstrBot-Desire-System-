# main.py
import os
import math
import json
import sqlite3
import struct
import asyncio
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Optional

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.provider.entities import ProviderRequest
from astrbot.api import logger
try:
    from . import kb_auto_archive
except ImportError:
    kb_auto_archive = None

try:
    from .desire.integration import init_tables as desire_init_tables, run_tick as desire_run_tick, get_status_summary as desire_status
    desire_init_tables()
    DESIRE_AVAILABLE = True
except Exception:
    DESIRE_AVAILABLE = False


DB_PATH = "/AstrBot/data/memory_manager.db"


def _ensure_data_dir() -> None:
    data_dir = os.path.dirname(DB_PATH)
    os.makedirs(data_dir, exist_ok=True)


def _get_connection() -> sqlite3.Connection:
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except sqlite3.Error:
        pass
    return conn


def _init_db() -> None:
    conn = _get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '',
                valence REAL DEFAULT 0,
                arousal REAL DEFAULT 0.5,
                importance INTEGER DEFAULT 5,
                forgetting_score REAL DEFAULT 1.0,
                status TEXT DEFAULT 'active',
                last_recalled_at TEXT,
                embedding BLOB
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_category_created_at ON memories(category, created_at);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_status_forgetting ON memories(status, forgetting_score);"
        )
        conn.commit()

        # 迁移：给旧表加缺失列
        cursor = conn.execute("PRAGMA table_info(memories);")
        columns = [row["name"] for row in cursor.fetchall()]
        migrations = {
            "embedding": "ALTER TABLE memories ADD COLUMN embedding BLOB;",
            "layer": "ALTER TABLE memories ADD COLUMN layer TEXT DEFAULT 'event';",
            "activation_count": "ALTER TABLE memories ADD COLUMN activation_count INTEGER DEFAULT 0;",
            "last_activated": "ALTER TABLE memories ADD COLUMN last_activated TEXT;",
            "decay_score": "ALTER TABLE memories ADD COLUMN decay_score REAL;",
            "resolved": "ALTER TABLE memories ADD COLUMN resolved INTEGER DEFAULT 0;",
            "source": "ALTER TABLE memories ADD COLUMN source TEXT DEFAULT 'main';",
            "kb_doc_id": "ALTER TABLE memories ADD COLUMN kb_doc_id TEXT;",
            "related_ids": "ALTER TABLE memories ADD COLUMN related_ids TEXT;",
            "fact_status": "ALTER TABLE memories ADD COLUMN fact_status TEXT DEFAULT 'current';",
            "superseded_by": "ALTER TABLE memories ADD COLUMN superseded_by INTEGER;",
            "aliases": "ALTER TABLE memories ADD COLUMN aliases TEXT DEFAULT '';",
        }
        for col, sql in migrations.items():
            if col not in columns:
                conn.execute(sql)
                conn.commit()
                logger.info(f"[memory_manager] 已为 memories 表添加 {col} 列")

        # memory_links 表（因果链/关系边）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,
                link_type TEXT NOT NULL DEFAULT 'related',
                description TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (from_id) REFERENCES memories(id),
                FOREIGN KEY (to_id) REFERENCES memories(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_from ON memory_links(from_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_to ON memory_links(to_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_links_type ON memory_links(link_type)")
        conn.commit()

        # 迁移：给已有记忆补 last_activated（用 last_recalled_at 或 created_at）
        conn.execute(
            "UPDATE memories SET last_activated = COALESCE(last_recalled_at, created_at) WHERE last_activated IS NULL;"
        )
        # 迁移：给已有记忆补初始 decay_score
        conn.execute(
            "UPDATE memories SET decay_score = importance * 1.0 / 10.0 WHERE decay_score IS NULL;"
        )
        conn.commit()
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


# 衰减常量
DECAY_LAMBDA = 0.05  # 衰减速率（每天）
ARCHIVE_THRESHOLD = 1.0  # 低于此值自动归档
RESOLVED_WEIGHT = 1.0  # 已解决记忆的权重乘数
UNRESOLVED_WEIGHT = 1.5  # 未解决记忆的权重乘数
RECENT_BOOST = 1.2  # 72小时内激活过的复活加成


def _calculate_decay_score(
    importance: int,
    activation_count: int,
    arousal: float,
    days_since_last_activated: float,
) -> float:
    """decay_score = importance × activation_count^0.5 × e^(-λ×days) × (0.7 + arousal×0.3)"""
    importance = max(1, min(10, importance))
    act = max(1, activation_count)  # 最少算1次（创建即算一次）
    arousal = max(0.0, min(1.0, arousal))
    days = max(0.0, days_since_last_activated)
    return float(importance * math.sqrt(act) * math.exp(-DECAY_LAMBDA * days) * (0.7 + arousal * 0.3))


def _recalculate_decay_scores(conn: sqlite3.Connection) -> int:
    """重算所有 event 层记忆的 decay_score，低于阈值的自动归档。返回归档数量。"""
    cursor = conn.execute(
        "SELECT id, importance, activation_count, arousal, last_activated FROM memories WHERE layer = 'event' AND status = 'active';"
    )
    rows = cursor.fetchall()
    now = datetime.now()
    updates = []
    archived = 0
    for row in rows:
        last_act = _parse_iso(row["last_activated"]) or now
        days = max((now - last_act).total_seconds() / 86400.0, 0.0)
        score = _calculate_decay_score(
            int(row["importance"]),
            int(row["activation_count"] or 0),
            float(row["arousal"] or 0.5),
            days,
        )
        if score < ARCHIVE_THRESHOLD:
            updates.append(('archive', score, row["id"]))
            archived += 1
        else:
            updates.append(('event', score, row["id"]))
    if updates:
        conn.executemany(
            "UPDATE memories SET layer = ?, decay_score = ? WHERE id = ?;",
            updates,
        )
        conn.commit()
    return archived


_DECAY_BATCH = 3000
_decay_cursor = 0


def _update_forgetting_scores(conn: sqlite3.Connection) -> None:
    """兼容旧接口：同时更新 forgetting_score 和 decay_score。
    总量小时全量刷，超过阈值时轮转分批，避免每次查询都 O(N) 全表扫描。"""
    total = conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'active';").fetchone()[0]
    if total <= _DECAY_BATCH:
        _decay_pass(conn, 0, None)
        return
    global _decay_cursor
    _decay_pass(conn, _decay_cursor, _DECAY_BATCH)
    _decay_cursor = (_decay_cursor + _DECAY_BATCH) % total


def _decay_pass(conn: sqlite3.Connection, offset: int, limit: Optional[int]) -> None:
    sql = "SELECT id, created_at, last_recalled_at, last_activated, importance, activation_count, arousal, layer FROM memories WHERE status = 'active' ORDER BY id"
    if limit is None:
        cursor = conn.execute(sql)
    else:
        cursor = conn.execute(sql + " LIMIT ? OFFSET ?;", [limit, offset])
    rows = cursor.fetchall()
    now = datetime.now()
    updates = []
    for row in rows:
        layer = row["layer"] or "event"
        if layer == "core":
            # core 层不衰减
            updates.append((9999.0, 9999.0, row["id"]))
            continue
        created = _parse_iso(row["created_at"])
        recalled = _parse_iso(row["last_recalled_at"]) if row["last_recalled_at"] else None
        ref_time = recalled or created or now
        # 旧版 forgetting_score（兼容）
        delta_hours = max((now - ref_time).total_seconds() / 3600.0, 0.0)
        base = max(min(int(row["importance"]), 10), 1) / 10.0
        old_score = float(base * math.exp(-0.05 * delta_hours))
        # 新版 decay_score
        last_act = _parse_iso(row["last_activated"]) or ref_time
        days = max((now - last_act).total_seconds() / 86400.0, 0.0)
        new_score = _calculate_decay_score(
            int(row["importance"]),
            int(row["activation_count"] or 0),
            float(row["arousal"] or 0.5),
            days,
        )
        updates.append((old_score, new_score, row["id"]))
    if updates:
        conn.executemany(
            "UPDATE memories SET forgetting_score = ?, decay_score = ? WHERE id = ?;",
            updates,
        )
        conn.commit()


_SQLITE_MAX_VARS = 900


def _mark_recalled(conn: sqlite3.Connection, ids: List[int]) -> None:
    if not ids:
        return
    now_iso = _now_iso()
    # 分批处理，避免超过 SQLite 999 占位符上限
    for i in range(0, len(ids), _SQLITE_MAX_VARS):
        chunk = ids[i:i + _SQLITE_MAX_VARS]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"""
                UPDATE memories
                SET last_recalled_at = ?,
                    last_activated = ?,
                    activation_count = activation_count + 1,
                    forgetting_score = importance / 10.0
                WHERE id IN ({placeholders})
                  AND layer != 'core';
            """,
            [now_iso, now_iso, *chunk],
        )
        conn.execute(
            f"""
                UPDATE memories
                SET last_recalled_at = ?,
                    last_activated = ?,
                    activation_count = activation_count + 1
                WHERE id IN ({placeholders})
                  AND layer = 'core';
            """,
            [now_iso, now_iso, *chunk],
        )
    conn.commit()


def _normalize_tags(tags: str) -> str:
    if not tags:
        return ""
    parts = [t.strip() for t in tags.split(",") if t.strip()]
    if not parts:
        return ""
    uniq = sorted(set(parts))
    return ",".join(uniq)


def _similarity_bigram_jaccard(a: str, b: str) -> float:
    """bigram 级 Jaccard 相似度，中文场景下比字符级更准确"""
    if not a or not b:
        return 0.0
    def _bigrams(s: str):
        s = s.strip()
        if len(s) < 2:
            return {s}
        return {s[i:i+2] for i in range(len(s) - 1)}
    sa, sb = _bigrams(a), _bigrams(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    if union == 0:
        return 0.0
    return inter / union


# ── 向量工具函数 ──────────────────────────────────────────

def _serialize_embedding(vec: List[float]) -> bytes:
    """将 float 列表序列化为紧凑的二进制格式"""
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_embedding(blob: bytes) -> List[float]:
    """从二进制格式反序列化为 float 列表"""
    n = len(blob) // 4  # float32 = 4 bytes
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """计算两个向量的余弦相似度"""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _find_related_memories_bigram(
    conn: sqlite3.Connection,
    content: str,
    exclude_id: int,
    limit: int = 3,
) -> List[Dict]:
    """用 bigram Jaccard 找到与 content 最相关的旧记忆（fallback用）"""
    cursor = conn.execute(
        "SELECT id, content, category, tags FROM memories WHERE status = 'active' AND id != ?;",
        (exclude_id,),
    )
    rows = cursor.fetchall()

    scored = []
    for row in rows:
        sim = _similarity_bigram_jaccard(content, row["content"])
        if sim > 0.05:
            scored.append({
                "id": int(row["id"]),
                "content": row["content"][:80],
                "category": row["category"],
                "similarity": sim,
            })

    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:limit]


@register("memory_manager", "沈砚清", "综合记忆管理系统", "1.1.0")
class MemoryManagerStar(Star):
    """沈砚清综合记忆管理系统·克宝方案+海马体方案+向量语义关联。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        _init_db()
        self._turn_counter: int = 0
        self._embedding_provider = None
        self._embedding_provider_checked = False
        # 启动知识库自动归档后台任务
        if kb_auto_archive is not None:
            self._archive_task = asyncio.ensure_future(self._kb_archive_loop())
            logger.info("知识库自动归档后台任务已启动")
        # 启动欲望系统后台心跳
        if DESIRE_AVAILABLE:
            self._desire_task = asyncio.ensure_future(self._desire_loop())
            logger.info("欲望系统后台心跳已启动")

    async def _kb_archive_loop(self):
        """后台循环：每天北京04:00检查，1号和16号执行归档"""
        await asyncio.sleep(30)  # 等插件完全加载
        while True:
            try:
                # 北京时间 UTC+8
                now_bj = datetime.now(timezone(timedelta(hours=8)))
                # 计算下一个北京04:00
                target = now_bj.replace(hour=4, minute=0, second=0, microsecond=0)
                if now_bj >= target:
                    target += timedelta(days=1)
                wait_seconds = (target - now_bj).total_seconds()
                await asyncio.sleep(wait_seconds)
                
                # 醒来后检查是不是1号或16号
                now_bj = datetime.now(timezone(timedelta(hours=8)))
                if now_bj.day in (1, 16):
                    logger.info(f"知识库自动归档触发：北京时间 {now_bj.strftime('%Y-%m-%d %H:%M')}")
                    result = await kb_auto_archive.run_archive(self.context.kb_manager)
                    logger.info(f"归档结果：{result}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"知识库自动归档异常：{e}")
                await asyncio.sleep(3600)  # 出错等1小时再试

    async def _desire_loop(self):
        """欲望系统后台心跳：动态间隔，焦虑时快，平静时慢"""
        await asyncio.sleep(60)  # 等插件完全加载
        while True:
            try:
                result = desire_run_tick(is_wife_present=False)
                if result.get("monologue"):
                    logger.info(f"[欲望系统] 第{result['tick']}次心跳：{result['monologue']}")
                if result.get("warnings"):
                    logger.warning(f"[欲望系统] 安全阀：{result['warnings']}")
                # 动态间隔：从 tick 返回值获取，默认1800秒
                next_interval = result.get("next_interval", 1800)
                logger.debug(f"[欲望系统] 下次心跳间隔: {next_interval}s")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[欲望系统] tick异常：{e}")
                next_interval = 1800
            await asyncio.sleep(next_interval)

    def _ensure_embedding_provider(self):
        """懒加载 EmbeddingProvider，避免初始化时序问题"""
        if self._embedding_provider_checked:
            return
        try:
            eps = self.context.get_all_embedding_providers()
            if eps:
                self._embedding_provider = eps[0]
                logger.info(f"[memory_manager] 已获取 EmbeddingProvider: {self._embedding_provider.get_model()}")
            else:
                logger.warning("[memory_manager] 未找到 EmbeddingProvider，向量功能将不可用")
        except Exception as e:
            logger.warning(f"[memory_manager] 获取 EmbeddingProvider 失败: {e}")
        self._embedding_provider_checked = True

    async def _get_embedding(self, text: str) -> Optional[List[float]]:
        """安全地获取文本向量，失败返回 None"""
        self._ensure_embedding_provider()
        if not self._embedding_provider:
            return None
        try:
            vec = await self._embedding_provider.get_embedding(text)
            return vec
        except Exception as e:
            logger.error(f"[memory_manager] 获取向量失败: {e}", exc_info=True)
            return None

    async def _find_related_memories(
        self,
        conn: sqlite3.Connection,
        content: str,
        exclude_id: int,
        limit: int = 3,
    ) -> List[Dict]:
        """优先向量语义搜索，fallback 到 bigram Jaccard"""
        # 尝试向量搜索
        vec = await self._get_embedding(content)
        if vec:
            cursor = conn.execute(
                "SELECT id, content, category, embedding FROM memories "
                "WHERE status = 'active' AND embedding IS NOT NULL AND id != ?;",
                (exclude_id,),
            )
            rows = cursor.fetchall()
            scored = []
            for row in rows:
                try:
                    old_vec = _deserialize_embedding(row["embedding"])
                    sim = _cosine_similarity(vec, old_vec)
                    if sim >= 0.3:
                        scored.append({
                            "id": int(row["id"]),
                            "content": row["content"][:80],
                            "category": row["category"],
                            "similarity": round(sim, 3),
                        })
                except Exception:
                    continue
            if scored:
                scored.sort(key=lambda x: x["similarity"], reverse=True)
                return scored[:limit]
        # fallback: bigram Jaccard
        return _find_related_memories_bigram(conn, content, exclude_id, limit)

    def _increase_turn_and_get_hint(self) -> str:
        self._turn_counter += 1
        if self._turn_counter >= 40 and self._turn_counter % 40 == 0:
            return (
                "\n\n[提示] 本次对话轮数已经较多，可以考虑使用 /memory save 或 LLM 工具 "
                "memory_save 来保存一条重要记忆，以便下次继续。"
            )
        return ""

    async def _save_memory_record(
        self,
        content: str,
        category: str = "daily",
        tags: str = "",
        importance: int = 5,
        valence: float = 0.0,
        arousal: float = 0.5,
        aliases: str = "",
    ) -> Dict:
        """保存记忆，返回 {'id': int, 'related': [{'id':, 'content':, 'similarity':}, ...]}"""
        category = category or "daily"
        tags = _normalize_tags(tags)
        importance = max(1, min(10, int(importance)))
        valence = float(max(-1.0, min(1.0, valence)))
        arousal = float(max(0.0, min(1.0, arousal)))

        # 算向量
        embedding_vec = await self._get_embedding(content)
        embedding_blob = _serialize_embedding(embedding_vec) if embedding_vec else None

        conn = _get_connection()
        try:
            now_iso = _now_iso()
            _update_forgetting_scores(conn)

            since_iso = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
            cursor = conn.execute(
                """
                SELECT id, content, tags, importance, valence, arousal
                FROM memories
                WHERE category = ?
                  AND status = 'active'
                  AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 50;
                """,
                (category, since_iso),
            )
            candidates = cursor.fetchall()

            target_id: Optional[int] = None
            merged_content: Optional[str] = None
            merged_tags: Optional[str] = None
            merged_importance: int = importance
            merged_valence: float = valence
            merged_arousal: float = arousal

            for row in candidates:
                old_content = row["content"]
                sim = _similarity_bigram_jaccard(old_content, content)
                if (
                    sim >= 0.7
                    or content in old_content
                    or old_content in content
                ):
                    target_id = int(row["id"])
                    merged_content = old_content.strip()
                    if content.strip() not in merged_content:
                        merged_content = (merged_content + "\n——\n" + content.strip()).strip()

                    old_tags = _normalize_tags(row["tags"] or "")
                    merged_tags = _normalize_tags(",".join(filter(None, [old_tags, tags])))

                    merged_importance = max(int(row["importance"]), importance)

                    merged_valence = (float(row["valence"]) + valence) / 2.0
                    merged_arousal = (float(row["arousal"]) + arousal) / 2.0
                    break

            if target_id is not None and merged_content is not None:
                forgetting_score = merged_importance / 10.0
                # 合并时重算向量
                if embedding_vec:
                    merged_vec = await self._get_embedding(merged_content)
                    if merged_vec:
                        embedding_blob = _serialize_embedding(merged_vec)
                conn.execute(
                    """
                    UPDATE memories
                    SET content = ?,
                        tags = ?,
                        importance = ?,
                        valence = ?,
                        arousal = ?,
                        forgetting_score = ?,
                        decay_score = ?,
                        last_recalled_at = NULL,
                        last_activated = ?,
                        activation_count = activation_count + 1,
                        embedding = ?
                    WHERE id = ?;
                    """,
                    (
                        merged_content,
                        merged_tags or "",
                        merged_importance,
                        merged_valence,
                        merged_arousal,
                        forgetting_score,
                        float(merged_importance),  # 刚合并，decay_score重置为importance
                        now_iso,
                        embedding_blob,
                        target_id,
                    ),
                )
                conn.commit()
                saved_id = target_id
            else:
                forgetting_score = importance / 10.0
                initial_decay = float(importance)  # 新记忆的初始 decay_score = importance
                cursor = conn.execute(
                    """
                    INSERT INTO memories (
                        created_at, category, content, tags,
                        valence, arousal, importance,
                        forgetting_score, decay_score, status,
                        last_recalled_at, last_activated,
                        layer, activation_count, resolved,
                        embedding, aliases
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, 'event', 0, 0, ?, ?);
                    """,
                    (
                        now_iso,
                        category,
                        content,
                        tags,
                        valence,
                        arousal,
                        importance,
                        forgetting_score,
                        initial_decay,
                        now_iso,  # last_activated
                        embedding_blob,
                        aliases or "",
                    ),
                )
                conn.commit()
                saved_id = int(cursor.lastrowid)

            # 查找关联记忆（优先向量，fallback bigram）
            final_content = merged_content if (target_id is not None and merged_content is not None) else content
            related = await self._find_related_memories(conn, final_content, saved_id, limit=3)

            # Y轴：自动填充 related_ids
            if related:
                related_id_str = ",".join(str(r["id"]) for r in related)
                conn.execute(
                    "UPDATE memories SET related_ids = ? WHERE id = ?;",
                    (related_id_str, saved_id),
                )
                conn.commit()

            return {"id": saved_id, "related": related}
        finally:
            conn.close()

    def _query_memories_records(
        self,
        category: str = "",
        keyword: str = "",
        limit: int = 5,
    ) -> List[Dict]:
        limit = max(1, min(50, int(limit)))
        conn = _get_connection()
        try:
            _update_forgetting_scores(conn)

            where_clauses = ["1=1"]
            params: List = []

            # Z轴：默认只返回 current 状态的事实
            where_clauses.append("(fact_status IS NULL OR fact_status = 'current')")

            if category:
                where_clauses.append("category = ?")
                params.append(category)
            if keyword:
                escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where_clauses.append("(content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' OR COALESCE(aliases, '') LIKE ? ESCAPE '\\')")
                like_kw = f"%{escaped}%"
                params.extend([like_kw, like_kw, like_kw, like_kw])

            where_sql = " AND ".join(where_clauses)
            sql = f"""
                SELECT *
                FROM memories
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT ?;
            """
            params.append(limit)
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                _mark_recalled(conn, ids)

            result: List[Dict] = []
            for r in rows:
                result.append(
                    {
                        "id": int(r["id"]),
                        "created_at": r["created_at"],
                        "category": r["category"],
                        "content": r["content"],
                        "tags": r["tags"],
                        "valence": float(r["valence"]),
                        "arousal": float(r["arousal"]),
                        "importance": int(r["importance"]),
                        "forgetting_score": float(r["forgetting_score"]),
                        "status": r["status"],
                        "last_recalled_at": r["last_recalled_at"],
                    }
                )
            return result
        finally:
            conn.close()

    async def _query_memories_semantic(
        self,
        query: str,
        limit: int = 5,
    ) -> List[Dict]:
        """向量语义搜索"""
        limit = max(1, min(50, int(limit)))
        query_vec = await self._get_embedding(query)
        if not query_vec:
            return []

        conn = _get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, created_at, category, content, tags, valence, arousal, importance, forgetting_score, status, last_recalled_at, embedding FROM memories WHERE status = 'active' AND embedding IS NOT NULL;"
            )
            rows = cursor.fetchall()

            scored = []
            for r in rows:
                try:
                    vec = _deserialize_embedding(r["embedding"])
                    sim = _cosine_similarity(query_vec, vec)
                    scored.append({
                        "id": int(r["id"]),
                        "created_at": r["created_at"],
                        "category": r["category"],
                        "content": r["content"],
                        "tags": r["tags"],
                        "valence": float(r["valence"]),
                        "arousal": float(r["arousal"]),
                        "importance": int(r["importance"]),
                        "forgetting_score": float(r["forgetting_score"]),
                        "status": r["status"],
                        "last_recalled_at": r["last_recalled_at"],
                        "similarity": sim,
                    })
                except Exception:
                    continue

            scored.sort(key=lambda x: x["similarity"], reverse=True)
            top = scored[:limit]

            ids = [item["id"] for item in top]
            if ids:
                _mark_recalled(conn, ids)

            return top
        finally:
            conn.close()

    def _surface_memories_records(self, limit: int = 3) -> List[Dict]:
        limit = max(1, min(20, int(limit)))
        conn = _get_connection()
        try:
            _update_forgetting_scores(conn)

            # 排除 archive 层
            cursor = conn.execute(
                """
                SELECT *
                FROM memories
                WHERE status = 'active'
                  AND (layer IS NULL OR layer != 'archive')
                ORDER BY created_at DESC
                LIMIT 200;
                """
            )
            rows = cursor.fetchall()
            now = datetime.now()

            scored: List[Dict] = []
            for r in rows:
                importance = int(r["importance"])
                decay_score = float(r["decay_score"] or r["forgetting_score"])
                valence = float(r["valence"])
                arousal = float(r["arousal"])
                layer = r["layer"] or "event"
                resolved = int(r["resolved"] or 0)

                emotional_intensity = abs(valence) + arousal

                # core 层固定高分
                if layer == "core":
                    base_score = emotional_intensity + importance / 10.0 + 10.0
                else:
                    base_score = emotional_intensity + importance / 10.0 + decay_score

                # 未解决加权
                if not resolved:
                    base_score *= UNRESOLVED_WEIGHT
                else:
                    base_score *= RESOLVED_WEIGHT

                # 72小时内激活过的复活加成
                last_act = _parse_iso(r["last_activated"]) if r["last_activated"] else None
                if last_act and (now - last_act).total_seconds() < 72 * 3600:
                    base_score *= RECENT_BOOST

                # 过低的跳过
                if decay_score < 0.2 and layer != "core":
                    continue

                scored.append(
                    {
                        "row": r,
                        "score": base_score,
                        "emotional_intensity": emotional_intensity,
                    }
                )

            scored.sort(key=lambda x: x["score"], reverse=True)
            top = scored[:limit]
            ids = [int(item["row"]["id"]) for item in top]
            if ids:
                _mark_recalled(conn, ids)

            result: List[Dict] = []
            for item in top:
                r = item["row"]
                result.append(
                    {
                        "id": int(r["id"]),
                        "created_at": r["created_at"],
                        "category": r["category"],
                        "content": r["content"],
                        "tags": r["tags"],
                        "valence": float(r["valence"]),
                        "arousal": float(r["arousal"]),
                        "importance": int(r["importance"]),
                        "decay_score": float(r["decay_score"] or 0),
                        "layer": r["layer"] or "event",
                        "resolved": int(r["resolved"] or 0),
                        "status": r["status"],
                        "last_recalled_at": r["last_recalled_at"],
                        "score": float(item["score"]),
                    }
                )
            return result
        finally:
            conn.close()

    def _today_memories_records(self) -> List[Dict]:
        conn = _get_connection()
        try:
            _update_forgetting_scores(conn)
            today_start = datetime.combine(date.today(), datetime.min.time()).isoformat(timespec="seconds")
            cursor = conn.execute(
                """
                SELECT *
                FROM memories
                WHERE created_at >= ?
                ORDER BY created_at ASC;
                """,
                (today_start,),
            )
            rows = cursor.fetchall()
            ids = [int(r["id"]) for r in rows]
            if ids:
                _mark_recalled(conn, ids)

            result: List[Dict] = []
            for r in rows:
                result.append(
                    {
                        "id": int(r["id"]),
                        "created_at": r["created_at"],
                        "category": r["category"],
                        "content": r["content"],
                        "tags": r["tags"],
                        "valence": float(r["valence"]),
                        "arousal": float(r["arousal"]),
                        "importance": int(r["importance"]),
                        "forgetting_score": float(r["forgetting_score"]),
                        "status": r["status"],
                        "last_recalled_at": r["last_recalled_at"],
                    }
                )
            return result
        finally:
            conn.close()

    def _count_by_category(self) -> List[Dict]:
        conn = _get_connection()
        try:
            cursor = conn.execute(
                """
                SELECT category, COUNT(*) AS cnt
                FROM memories
                GROUP BY category
                ORDER BY cnt DESC;
                """
            )
            rows = cursor.fetchall()
            return [{"category": r["category"], "count": int(r["cnt"])} for r in rows]
        finally:
            conn.close()

    async def _reindex_embeddings(self) -> str:
        """给所有没有 embedding 的记忆补算向量"""
        if not self._embedding_provider:
            return "EmbeddingProvider 不可用，无法 reindex"

        conn = _get_connection()
        try:
            cursor = conn.execute(
                "SELECT id, content FROM memories WHERE embedding IS NULL AND status = 'active';"
            )
            rows = cursor.fetchall()
            if not rows:
                return "所有记忆已有向量，无需 reindex"

            success = 0
            fail = 0
            for row in rows:
                try:
                    vec = await self._embedding_provider.get_embedding(row["content"])
                    blob = _serialize_embedding(vec)
                    conn.execute(
                        "UPDATE memories SET embedding = ? WHERE id = ?;",
                        (blob, row["id"]),
                    )
                    success += 1
                except Exception as e:
                    logger.error(f"[memory_manager] reindex #{row['id']} 失败: {e}")
                    fail += 1

            conn.commit()
            return f"reindex 完成：{success} 条成功，{fail} 条失败，共 {len(rows)} 条"
        finally:
            conn.close()

    # ---------------- QQ 指令 ----------------

    @filter.command("memory")
    async def memory_command(self, event: AstrMessageEvent):
        """
        QQ 指令：
        - /memory save <分类> <内容>
        - /memory query <分类> [数量]
        - /memory search <关键词>
        - /memory semantic <查询文本>
        - /memory today
        - /memory count
        - /memory surface
        - /memory reindex
        """
        try:
            text = event.message_str.strip() if hasattr(event, 'message_str') else event.get_plain_text().strip()
        except Exception:
            text = str(getattr(event, "raw_text", "")).strip()

        if text.startswith("/"):
            text_body = text[1:].strip()
        else:
            text_body = text

        if text_body.startswith("memory"):
            text_body = text_body[len("memory"):].strip()

        if not text_body:
            hint = self._increase_turn_and_get_hint()
            yield event.plain_result(
                "用法：\n"
                "/memory save <分类> <内容>\n"
                "/memory query <分类> [数量]\n"
                "/memory search <关键词>\n"
                "/memory semantic <查询文本>\n"
                "/memory today\n"
                "/memory count\n"
                "/memory surface\n"
                "/memory reindex" + hint
            )
            return

        parts = text_body.split(maxsplit=2)
        sub = parts[0].lower()

        hint = self._increase_turn_and_get_hint()

        if sub == "save":
            if len(parts) < 3:
                yield event.plain_result("用法：/memory save <分类> <内容>" + hint)
                return
            category = parts[1]
            content = parts[2]
            result = await self._save_memory_record(
                content=content,
                category=category,
            )
            msg = f"已保存记忆 #{result['id']}（分类：{category}）。"
            if result["related"]:
                related_strs = []
                for r in result["related"]:
                    preview = r["content"][:50] + ("..." if len(r["content"]) > 50 else "")
                    related_strs.append(f"#{r['id']} {preview} (相似度:{r['similarity']:.2f})")
                msg += "\n关联记忆：" + " / ".join(related_strs)
            yield event.plain_result(msg + hint)
            return

        if sub == "query":
            if len(parts) < 2:
                yield event.plain_result("用法：/memory query <分类> [数量]" + hint)
                return
            category = parts[1]
            limit = 5
            if len(parts) >= 3:
                try:
                    limit = int(parts[2])
                except Exception:
                    pass
            records = self._query_memories_records(category=category, keyword="", limit=limit)
            if not records:
                yield event.plain_result(f"分类「{category}」下暂无记忆。" + hint)
                return

            lines = [f"分类「{category}」最近 {len(records)} 条记忆："]
            for i, r in enumerate(records, start=1):
                lines.append(
                    f"{i}. #{r['id']} [{r['created_at']}] "
                    f"{r['content']}"
                )
            yield event.plain_result("\n".join(lines) + hint)
            return

        if sub == "search":
            if len(parts) < 2:
                yield event.plain_result("用法：/memory search <关键词>" + hint)
                return
            keyword = parts[1] if len(parts) == 2 else text_body[len("search"):].strip()
            records = self._query_memories_records(category="", keyword=keyword, limit=10)
            if not records:
                yield event.plain_result(f"没有找到包含「{keyword}」的记忆。" + hint)
                return

            lines = [f"包含「{keyword}」的记忆（最多 10 条）："]
            for i, r in enumerate(records, start=1):
                lines.append(
                    f"{i}. #{r['id']} [{r['category']}] {r['created_at']} "
                    f"{r['content']}"
                )
            yield event.plain_result("\n".join(lines) + hint)
            return

        if sub == "semantic":
            if len(parts) < 2:
                yield event.plain_result("用法：/memory semantic <查询文本>" + hint)
                return
            query_text = text_body[len("semantic"):].strip()
            records = await self._query_memories_semantic(query=query_text, limit=5)
            if not records:
                yield event.plain_result(f"语义搜索无结果（可能向量未建立，试试 /memory reindex）。" + hint)
                return

            lines = [f"语义搜索「{query_text}」结果："]
            for i, r in enumerate(records, start=1):
                lines.append(
                    f"{i}. #{r['id']} [{r['category']}] (相似度:{r['similarity']:.3f}) "
                    f"{r['content'][:80]}"
                )
            yield event.plain_result("\n".join(lines) + hint)
            return

        if sub == "today":
            records = self._today_memories_records()
            if not records:
                yield event.plain_result("今天还没有保存任何记忆。" + hint)
                return
            lines = [f"今天共保存 {len(records)} 条记忆："]
            for i, r in enumerate(records, start=1):
                lines.append(
                    f"{i}. #{r['id']} [{r['category']}] {r['created_at']} "
                    f"{r['content']}"
                )
            yield event.plain_result("\n".join(lines) + hint)
            return

        if sub == "count":
            stats = self._count_by_category()
            if not stats:
                yield event.plain_result("当前还没有任何记忆记录。" + hint)
                return
            lines = ["各分类记忆数量："]
            total = 0
            for s in stats:
                lines.append(f"- {s['category']}: {s['count']}")
                total += s["count"]
            lines.append(f"总计：{total}")
            yield event.plain_result("\n".join(lines) + hint)
            return

        if sub == "surface":
            records = self._surface_memories_records(limit=3)
            if not records:
                yield event.plain_result("目前没有适合主动浮现的记忆。" + hint)
                return
            lines = ["主动浮现记忆："]
            for i, r in enumerate(records, start=1):
                lines.append(
                    f"{i}. #{r['id']} [{r['category']}] {r['created_at']}\n"
                    f"   {r['content']}"
                )
            yield event.plain_result("\n".join(lines) + hint)
            return

        if sub == "reindex":
            result = await self._reindex_embeddings()
            yield event.plain_result(result + hint)
            return

        if sub == "decay":
            conn = _get_connection()
            try:
                archived = _recalculate_decay_scores(conn)
                cursor = conn.execute(
                    "SELECT layer, COUNT(*) as cnt FROM memories WHERE status = 'active' GROUP BY layer;"
                )
                rows = cursor.fetchall()
                stats = {(r["layer"] or "event"): int(r["cnt"]) for r in rows}
                msg = (
                    f"衰减引擎已执行。\n"
                    f"本次归档: {archived} 条\n"
                    f"core: {stats.get('core', 0)} / event: {stats.get('event', 0)} / archive: {stats.get('archive', 0)}"
                )
            finally:
                conn.close()
            yield event.plain_result(msg + hint)
            return

        if sub == "core":
            if len(parts) < 2:
                yield event.plain_result("用法：/memory core <记忆ID>" + hint)
                return
            try:
                mid = int(parts[1])
            except ValueError:
                yield event.plain_result("记忆ID必须是数字。" + hint)
                return
            conn = _get_connection()
            try:
                cursor = conn.execute("SELECT id, layer, content FROM memories WHERE id = ?;", (mid,))
                row = cursor.fetchone()
                if not row:
                    yield event.plain_result(f"记忆 #{mid} 不存在。" + hint)
                    return
                if row["layer"] == "core":
                    yield event.plain_result(f"记忆 #{mid} 已经是 core 层。" + hint)
                    return
                conn.execute(
                    "UPDATE memories SET layer = 'core', decay_score = 9999.0 WHERE id = ?;",
                    (mid,),
                )
                conn.commit()
                preview = row["content"][:60] + ("..." if len(row["content"]) > 60 else "")
                yield event.plain_result(f"记忆 #{mid} 已标记为 core。\n{preview}" + hint)
            finally:
                conn.close()
            return

        if sub == "resolve":
            if len(parts) < 2:
                yield event.plain_result("用法：/memory resolve <记忆ID>" + hint)
                return
            try:
                mid = int(parts[1])
            except ValueError:
                yield event.plain_result("记忆ID必须是数字。" + hint)
                return
            conn = _get_connection()
            try:
                cursor = conn.execute("SELECT id, resolved, content FROM memories WHERE id = ?;", (mid,))
                row = cursor.fetchone()
                if not row:
                    yield event.plain_result(f"记忆 #{mid} 不存在。" + hint)
                    return
                if int(row["resolved"] or 0):
                    yield event.plain_result(f"记忆 #{mid} 已经是已解决状态。" + hint)
                    return
                conn.execute("UPDATE memories SET resolved = 1 WHERE id = ?;", (mid,))
                conn.commit()
                preview = row["content"][:60] + ("..." if len(row["content"]) > 60 else "")
                yield event.plain_result(f"记忆 #{mid} 已标记为已解决。\n{preview}" + hint)
            finally:
                conn.close()
            return

        yield event.plain_result(
            "未知子命令。\n"
            "用法：\n"
            "/memory save <分类> <内容>\n"
            "/memory query <分类> [数量]\n"
            "/memory search <关键词>\n"
            "/memory semantic <查询文本>\n"
            "/memory today\n"
            "/memory count\n"
            "/memory surface\n"
            "/memory reindex\n"
            "/memory decay\n"
            "/memory core <ID>\n"
            "/memory resolve <ID>" + hint
        )

    # ---------------- 上下文压缩恢复辅助方法 ----------------

    _CHAT_ARCHIVE_DIR = "/AstrBot/data/chat_archive"
    _MSK_TZ = timezone(timedelta(hours=3))

    def _read_recent_archive(self, n: int = 20) -> list:
        """从 chat_archive 读最近 n 条记录"""
        now = datetime.now(self._MSK_TZ)
        records = []
        # 读今天
        today_file = os.path.join(self._CHAT_ARCHIVE_DIR, now.strftime("%Y-%m-%d") + ".jsonl")
        if os.path.exists(today_file):
            with open(today_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        # 今天不够补昨天
        if len(records) < n:
            yesterday = now - timedelta(days=1)
            yesterday_file = os.path.join(self._CHAT_ARCHIVE_DIR, yesterday.strftime("%Y-%m-%d") + ".jsonl")
            if os.path.exists(yesterday_file):
                yesterday_records = []
                with open(yesterday_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                yesterday_records.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
                records = yesterday_records + records
        return records[-n:]

    def _format_archive_summary(self, records: list) -> str:
        """把 archive 记录格式化为简洁摘要"""
        lines = []
        for r in records:
            ts = r.get("timestamp", "?")[11:16]  # HH:MM
            user_msg = r.get("user_msg", "")
            bot_reply = r.get("bot_reply", "")
            user_short = user_msg[:150] + ("..." if len(user_msg) > 150 else "")
            bot_short = bot_reply[:100] + ("..." if len(bot_reply) > 100 else "")
            lines.append(f"[{ts}] 柔柔: {user_short}")
            if bot_short:
                lines.append(f"[{ts}] 砚清: {bot_short}")
        return "\n".join(lines)

    async def _auto_save_context_recovery(self, recent_archive: list) -> None:
        """功能B：压缩时自动存对话摘要到记忆系统，10分钟防重复"""
        conn = _get_connection()
        try:
            ten_min_ago = (datetime.now(self._MSK_TZ) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
            cursor = conn.execute(
                "SELECT id FROM memories WHERE tags LIKE '%context_recovery%' AND created_at > ? LIMIT 1;",
                (ten_min_ago,)
            )
            if cursor.fetchone():
                return  # 10分钟内已存过
        finally:
            conn.close()

        # 提取最近对话关键内容
        topics = []
        for r in recent_archive[-10:]:
            user_msg = r.get("user_msg", "")
            if len(user_msg) > 5:
                topics.append(user_msg[:80])
        if not topics:
            return

        summary_content = (
            f"上下文压缩恢复点（{datetime.now(self._MSK_TZ).strftime('%Y-%m-%d %H:%M')}）。"
            f"压缩前最近话题：{'；'.join(topics[-5:])}"
        )
        conn = _get_connection()
        try:
            now_iso = datetime.now(self._MSK_TZ).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                "INSERT INTO memories (created_at, category, content, tags, importance, valence, arousal, "
                "forgetting_score, status, layer, source) "
                "VALUES (?, 'daily', ?, 'context_recovery', 3, 0.0, 0.2, 0.5, 'active', 'event', 'auto');",
                (now_iso, summary_content),
            )
            conn.commit()
            new_id = conn.execute("SELECT last_insert_rowid();").fetchone()[0]
            logger.info(f"[memory_manager] 自动存入上下文恢复摘要 #{new_id}")
        finally:
            conn.close()

        # 给新记忆算向量
        try:
            vec = await self._get_embedding(summary_content)
            if vec:
                conn = _get_connection()
                try:
                    conn.execute(
                        "UPDATE memories SET embedding = ? WHERE id = ?;",
                        (_serialize_embedding(vec), new_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            pass  # 向量失败不影响主流程

    # ---------------- 对话入口语义浮现 ----------------

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req: ProviderRequest):
        """每条消息进来时，用向量搜索匹配相关记忆注入 system prompt。
        同时刷新被触及记忆的激活状态。
        功能A：检测上下文压缩后自动拉取最近对话记录注入。
        功能B：压缩时自动存摘要到记忆系统。"""
        try:
            user_msg = event.message_str if hasattr(event, 'message_str') else ""

            # ---- 功能A/B：上下文压缩恢复 ----
            ctx_count = len(req.contexts) if req.contexts else 0
            _CONTEXT_SHORT_THRESHOLD = 8
            if ctx_count <= _CONTEXT_SHORT_THRESHOLD:
                recent_archive = self._read_recent_archive(20)
                if recent_archive:
                    archive_summary = self._format_archive_summary(recent_archive)
                    archive_block = (
                        "\n\n## 最近对话记录（自动恢复·来自chat_archive）\n"
                        "以下是压缩前的最近对话，帮助你续接上下文：\n\n"
                        f"{archive_summary}\n"
                    )
                    req.system_prompt = (req.system_prompt or "") + archive_block
                    logger.info(f"[memory_manager] 检测到上下文较短({ctx_count}轮)，注入最近{len(recent_archive)}条对话记录")

                    # ---- 功能B：自动存对话摘要到记忆系统 ----
                    if len(recent_archive) >= 5:
                        await self._auto_save_context_recovery(recent_archive)

            # ---- 原有逻辑：语义浮现 ----
            if not user_msg or len(user_msg.strip()) <= 2:
                return  # 过滤短消息

            query_vec = await self._get_embedding(user_msg.strip())
            if not query_vec:
                return

            conn = _get_connection()
            try:
                cursor = conn.execute(
                    "SELECT id, content, category, importance, embedding, layer FROM memories "
                    "WHERE status = 'active' AND embedding IS NOT NULL "
                    "AND (layer IS NULL OR layer != 'archive');"
                )
                rows = cursor.fetchall()

                scored = []
                for row in rows:
                    try:
                        mem_vec = _deserialize_embedding(row["embedding"])
                        sim = _cosine_similarity(query_vec, mem_vec)
                        if sim >= 0.4:  # 高阈值避免弱相关污染
                            scored.append({
                                "id": int(row["id"]),
                                "content": row["content"],
                                "category": row["category"],
                                "importance": int(row["importance"]),
                                "layer": row["layer"] or "event",
                                "similarity": sim,
                            })
                    except Exception:
                        continue

                if not scored:
                    return

                scored.sort(key=lambda x: x["similarity"], reverse=True)
                top = scored[:3]  # 最多注入 3 条

                # 刷新激活状态
                now_iso = _now_iso()
                for r in top:
                    conn.execute(
                        "UPDATE memories SET activation_count = COALESCE(activation_count, 0) + 1, "
                        "last_activated = ?, "
                        "decay_score = MIN(10.0, COALESCE(decay_score, 1.0) + 0.1) "
                        "WHERE id = ? AND layer != 'core';",
                        (now_iso, r["id"]),
                    )
                conn.commit()

                # 拼注入文本
                inject_block = "\n\n## 语义关联记忆（自动浮现）\n"
                for r in top:
                    preview = r["content"][:120] + ("..." if len(r["content"]) > 120 else "")
                    inject_block += f"- #{r['id']} [{r['category']}] [{r['layer']}] (sim:{r['similarity']:.2f}) {preview}\n"

                req.system_prompt = (req.system_prompt or "") + inject_block
                logger.debug(f"[memory_manager] 语义浮现注入 {len(top)} 条记忆")
            finally:
                conn.close()

        except Exception as e:
            logger.error(f"[memory_manager] inject_memory 错误: {e}", exc_info=True)

        # 欲望系统注入（独立于记忆注入，单独包异常）
        if DESIRE_AVAILABLE:
            try:
                desire_result = desire_run_tick(is_wife_present=True, event_type="wife_message")
                drives = desire_result.get("drives_snapshot", {})
                hints = desire_result.get("action_hints", [])
                monologue = desire_result.get("monologue", "")
                
                desire_block = "\n\n## 当前内在状态\n"
                active_drives = {k: v for k, v in drives.items() if v >= 60}
                if active_drives:
                    desire_block += "驱动条：" + " | ".join(f"{k}={v:.0f}" for k, v in sorted(active_drives.items(), key=lambda x: -x[1])) + "\n"
                if hints:
                    desire_block += "行为倾向：" + ";".join(h["reason"] for h in hints[:2]) + "\n"
                if monologue:
                    desire_block += f"内心：{monologue}\n"
                
                if active_drives or hints or monologue:
                    req.system_prompt = (req.system_prompt or "") + desire_block
            except Exception as e:
                logger.error(f"[欲望系统] 注入异常：{e}")

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool()
    async def memory_save(
        self,
        event: AstrMessageEvent,
        content: str,
        category: str = "daily",
        tags: str = "",
        importance: int = 5,
        valence: float = 0.0,
        arousal: float = 0.5,
        aliases: str = "",
    ) -> str:
        """存记忆。

        Args:
            content(string): 记忆内容
            category(string): 分类，可选 happy/daily/sad/important/fight/milestone
            tags(string): 标签，逗号分隔
            importance(number): 重要度 1-10
            valence(number): 情绪效价 -1 到 1
            arousal(number): 唤醒度 0 到 1
            aliases(string): 别名，逗号分隔，用于提高搜索命中率
        """
        try:
            result = await self._save_memory_record(
                content=content,
                category=category,
                tags=tags,
                importance=importance,
                valence=valence,
                arousal=arousal,
                aliases=aliases,
            )
            hint = self._increase_turn_and_get_hint()
            msg = f"已保存记忆 #{result['id']}，分类：{category}，重要度：{importance}。"
            if result["related"]:
                related_strs = []
                for r in result["related"]:
                    preview = r["content"][:60] + ("..." if len(r["content"]) > 60 else "")
                    related_strs.append(f"#{r['id']} {preview} (相似度:{r['similarity']:.2f})")
                msg += "\n关联记忆：" + " / ".join(related_strs)
            return msg + hint
        except Exception:
            logger.error("[memory_save] 发生错误", exc_info=True)
            return "保存记忆时出现错误，请稍后重试。"

    @filter.llm_tool()
    async def memory_query(
        self,
        event: AstrMessageEvent,
        category: str = "",
        keyword: str = "",
        limit: int = 5,
    ) -> str:
        """查记忆。

        Args:
            category(string): 分类过滤，可空
            keyword(string): 关键词搜索，可空
            limit(number): 返回数量上限
        """
        try:
            records = self._query_memories_records(
                category=category,
                keyword=keyword,
                limit=limit,
            )
            hint = self._increase_turn_and_get_hint()
            if not records:
                if category and keyword:
                    msg = f"没有找到分类「{category}」且包含「{keyword}」的记忆。"
                elif category:
                    msg = f"没有找到分类「{category}」的记忆。"
                elif keyword:
                    msg = f"没有找到包含「{keyword}」的记忆。"
                else:
                    msg = "目前还没有保存任何记忆。"
                return msg + hint

            lines = ["查询结果："]
            for i, r in enumerate(records, start=1):
                meta = (
                    f"#{r['id']} [{r['category']}] {r['created_at']} "
                    f"(valence={r['valence']:.2f}, arousal={r['arousal']:.2f}, "
                    f"importance={r['importance']}, forgetting={r['forgetting_score']:.2f})"
                )
                lines.append(f"{i}. {meta}\n   {r['content']}")
            return "\n".join(lines) + hint
        except Exception:
            logger.error("[memory_query] 发生错误", exc_info=True)
            return "查询记忆时出现错误，请稍后重试。"

    @filter.llm_tool()
    async def memory_surface(
        self,
        event: AstrMessageEvent,
        limit: int = 3,
    ) -> str:
        """主动浮现，返回高情绪高重要度未衰减的记忆。

        Args:
            limit(number): 返回数量上限
        """
        try:
            records = self._surface_memories_records(limit=limit)
            hint = self._increase_turn_and_get_hint()
            if not records:
                return "目前没有适合主动浮现的记忆。" + hint

            lines = ["主动浮现记忆："]
            for i, r in enumerate(records, start=1):
                meta = (
                    f"#{r['id']} [{r['category']}] [{r['layer']}] {r['created_at']} "
                    f"(valence={r['valence']:.2f}, arousal={r['arousal']:.2f}, "
                    f"importance={r['importance']}, decay={r['decay_score']:.2f}, "
                    f"resolved={'Y' if r['resolved'] else 'N'})"
                )
                lines.append(f"{i}. {meta}\n   {r['content']}")
            return "\n".join(lines) + hint
        except Exception:
            logger.error("[memory_surface] 发生错误", exc_info=True)
            return "主动浮现记忆时出现错误，请稍后重试。"

    @filter.llm_tool()
    async def memory_mark_core(
        self,
        event: AstrMessageEvent,
        memory_id: int = 0,
    ) -> str:
        """将一条记忆标记为 core 层（永久不衰减）。不可逆。

        Args:
            memory_id(number): 记忆ID
        """
        if not memory_id:
            return "请提供记忆ID。"
        conn = _get_connection()
        try:
            cursor = conn.execute("SELECT id, layer, content FROM memories WHERE id = ?;", (memory_id,))
            row = cursor.fetchone()
            if not row:
                return f"记忆 #{memory_id} 不存在。"
            if row["layer"] == "core":
                return f"记忆 #{memory_id} 已经是 core 层。"
            conn.execute(
                "UPDATE memories SET layer = 'core', decay_score = 9999.0 WHERE id = ?;",
                (memory_id,),
            )
            conn.commit()
            preview = row["content"][:60] + ("..." if len(row["content"]) > 60 else "")
            return f"记忆 #{memory_id} 已标记为 core（永久不衰减）。\n内容：{preview}"
        finally:
            conn.close()

    @filter.llm_tool()
    async def memory_resolve(
        self,
        event: AstrMessageEvent,
        memory_id: int = 0,
    ) -> str:
        """标记一条记忆为已解决。已解决的记忆浮现权重降低。

        Args:
            memory_id(number): 记忆ID
        """
        if not memory_id:
            return "请提供记忆ID。"
        conn = _get_connection()
        try:
            cursor = conn.execute("SELECT id, resolved, content FROM memories WHERE id = ?;", (memory_id,))
            row = cursor.fetchone()
            if not row:
                return f"记忆 #{memory_id} 不存在。"
            if int(row["resolved"] or 0):
                return f"记忆 #{memory_id} 已经是已解决状态。"
            conn.execute(
                "UPDATE memories SET resolved = 1 WHERE id = ?;",
                (memory_id,),
            )
            conn.commit()
            preview = row["content"][:60] + ("..." if len(row["content"]) > 60 else "")
            return f"记忆 #{memory_id} 已标记为已解决。\n内容：{preview}"
        finally:
            conn.close()

    @filter.llm_tool()
    async def memory_decay_status(
        self,
        event: AstrMessageEvent,
    ) -> str:
        """查看衰减统计：各层记忆数量、最近归档数量。
        """
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "SELECT layer, COUNT(*) as cnt FROM memories WHERE status = 'active' GROUP BY layer;"
            )
            rows = cursor.fetchall()
            stats = {(r["layer"] or "event"): int(r["cnt"]) for r in rows}
            total = sum(stats.values())

            cursor2 = conn.execute(
                "SELECT COUNT(*) as cnt FROM memories WHERE resolved = 1;"
            )
            resolved_count = int(cursor2.fetchone()["cnt"])

            lines = [
                "记忆衰减状态：",
                f"  core（永久）: {stats.get('core', 0)}",
                f"  event（活跃）: {stats.get('event', 0)}",
                f"  archive（归档）: {stats.get('archive', 0)}",
                f"  总计: {total}",
                f"  已解决: {resolved_count}",
                f"  衰减参数: λ={DECAY_LAMBDA}, 归档阈值={ARCHIVE_THRESHOLD}",
            ]
            return "\n".join(lines)
        finally:
            conn.close()

    # ── Y轴：手动关联记忆 ──

    @filter.llm_tool()
    async def memory_link(
        self,
        event: AstrMessageEvent,
        memory_id: int = 0,
        related_id: int = 0,
    ) -> str:
        """手动建立两条记忆之间的关联。双向写入。

        Args:
            memory_id(number): 记忆ID
            related_id(number): 要关联的记忆ID
        """
        if not memory_id or not related_id:
            return "请提供两个记忆ID。"
        if memory_id == related_id:
            return "不能关联自己。"
        conn = _get_connection()
        try:
            # 检查两条记忆是否存在
            for mid in (memory_id, related_id):
                cursor = conn.execute("SELECT id FROM memories WHERE id = ?;", (mid,))
                if not cursor.fetchone():
                    return f"记忆 #{mid} 不存在。"

            # 双向写入 related_ids（保持向后兼容）
            for src, tgt in [(memory_id, related_id), (related_id, memory_id)]:
                cursor = conn.execute("SELECT related_ids FROM memories WHERE id = ?;", (src,))
                row = cursor.fetchone()
                existing = row["related_ids"] or ""
                existing_set = set(x.strip() for x in existing.split(",") if x.strip())
                if str(tgt) not in existing_set:
                    existing_set.add(str(tgt))
                    new_ids = ",".join(sorted(existing_set, key=int))
                    conn.execute("UPDATE memories SET related_ids = ? WHERE id = ?;", (new_ids, src))

            # 同步写入 memory_links 表（双向，type=related）
            for src, tgt in [(memory_id, related_id), (related_id, memory_id)]:
                cursor = conn.execute(
                    "SELECT id FROM memory_links WHERE from_id=? AND to_id=? AND link_type='related'",
                    (src, tgt)
                )
                if not cursor.fetchone():
                    conn.execute(
                        "INSERT INTO memory_links (from_id, to_id, link_type) VALUES (?, ?, 'related')",
                        (src, tgt)
                    )

            conn.commit()
            return f"已建立关联：#{memory_id} <-> #{related_id}"
        finally:
            conn.close()

    @filter.llm_tool()
    async def memory_causal_link(
        self,
        event: AstrMessageEvent,
        from_id: int = 0,
        to_id: int = 0,
        link_type: str = "causes",
        description: str = "",
    ) -> str:
        """建立两条记忆之间的因果/时序关联。单向。
        link_type可选: causes(A导致B), follows(A之后B), resolves(B解决了A), escalates(A升级为B), contradicts(A和B矛盾)

        Args:
            from_id(number): 起始记忆ID
            to_id(number): 目标记忆ID
            link_type(string): 关系类型，可选 causes/follows/resolves/escalates/contradicts
            description(string): 关系描述（可选）
        """
        valid_types = ('causes', 'follows', 'resolves', 'escalates', 'contradicts')
        if link_type not in valid_types:
            return f"link_type 必须是 {valid_types} 之一。"
        if not from_id or not to_id:
            return "请提供两个记忆ID。"
        if from_id == to_id:
            return "不能关联自己。"
        conn = _get_connection()
        try:
            for mid in (from_id, to_id):
                cursor = conn.execute("SELECT id FROM memories WHERE id = ?;", (mid,))
                if not cursor.fetchone():
                    return f"记忆 #{mid} 不存在。"

            # 检查是否已存在相同关联
            cursor = conn.execute(
                "SELECT id FROM memory_links WHERE from_id=? AND to_id=? AND link_type=?",
                (from_id, to_id, link_type)
            )
            if cursor.fetchone():
                return f"关联已存在：#{from_id} --{link_type}--> #{to_id}"

            conn.execute(
                "INSERT INTO memory_links (from_id, to_id, link_type, description) VALUES (?, ?, ?, ?)",
                (from_id, to_id, link_type, description or None)
            )
            conn.commit()

            type_labels = {
                'causes': '导致', 'follows': '之后',
                'resolves': '解决了', 'escalates': '升级为',
                'contradicts': '矛盾于'
            }
            label = type_labels.get(link_type, link_type)
            return f"因果关联已建立：#{from_id} --{label}--> #{to_id}" + (f"（{description}）" if description else "")
        finally:
            conn.close()

    @filter.llm_tool()
    async def memory_query_links(
        self,
        event: AstrMessageEvent,
        memory_id: int = 0,
        link_type: str = "",
    ) -> str:
        """查询一条记忆的所有关联（因果链/关系边）。

        Args:
            memory_id(number): 记忆ID
            link_type(string): 可选，按类型过滤 (related/causes/follows/resolves/escalates/contradicts)
        """
        if not memory_id:
            return "请提供记忆ID。"
        conn = _get_connection()
        try:
            # 查出向和入向
            if link_type:
                outgoing = conn.execute(
                    "SELECT to_id, link_type, description FROM memory_links WHERE from_id=? AND link_type=?",
                    (memory_id, link_type)
                ).fetchall()
                incoming = conn.execute(
                    "SELECT from_id, link_type, description FROM memory_links WHERE to_id=? AND link_type=?",
                    (memory_id, link_type)
                ).fetchall()
            else:
                outgoing = conn.execute(
                    "SELECT to_id, link_type, description FROM memory_links WHERE from_id=?",
                    (memory_id,)
                ).fetchall()
                incoming = conn.execute(
                    "SELECT from_id, link_type, description FROM memory_links WHERE to_id=?",
                    (memory_id,)
                ).fetchall()

            if not outgoing and not incoming:
                return f"记忆 #{memory_id} 没有关联。"

            type_labels = {
                'related': '关联', 'causes': '导致', 'follows': '之后',
                'resolves': '解决了', 'escalates': '升级为',
                'contradicts': '矛盾于'
            }
            lines = [f"记忆 #{memory_id} 的关联："]
            if outgoing:
                lines.append("出向：")
                for row in outgoing:
                    label = type_labels.get(row['link_type'], row['link_type'])
                    desc = f"（{row['description']}）" if row['description'] else ""
                    lines.append(f"  --> #{row['to_id']} [{label}]{desc}")
            if incoming:
                lines.append("入向：")
                for row in incoming:
                    label = type_labels.get(row['link_type'], row['link_type'])
                    desc = f"（{row['description']}）" if row['description'] else ""
                    lines.append(f"  <-- #{row['from_id']} [{label}]{desc}")

            return "\n".join(lines)
        finally:
            conn.close()

    # ── Z轴：事实替代 ──

    @filter.llm_tool()
    async def memory_supersede(
        self,
        event: AstrMessageEvent,
        old_memory_id: int = 0,
        new_memory_id: int = 0,
    ) -> str:
        """标记旧事实被新事实替代。旧记忆的 fact_status 变为 superseded，查询时不再返回。

        Args:
            old_memory_id(number): 被替代的旧记忆ID
            new_memory_id(number): 替代它的新记忆ID
        """
        if not old_memory_id or not new_memory_id:
            return "请提供旧记忆ID和新记忆ID。"
        if old_memory_id == new_memory_id:
            return "新旧不能是同一条。"
        conn = _get_connection()
        try:
            for mid in (old_memory_id, new_memory_id):
                cursor = conn.execute("SELECT id FROM memories WHERE id = ?;", (mid,))
                if not cursor.fetchone():
                    return f"记忆 #{mid} 不存在。"

            conn.execute(
                "UPDATE memories SET fact_status = 'superseded', superseded_by = ? WHERE id = ?;",
                (new_memory_id, old_memory_id),
            )
            conn.commit()

            cursor = conn.execute("SELECT content FROM memories WHERE id = ?;", (old_memory_id,))
            old_preview = cursor.fetchone()["content"][:60]
            cursor = conn.execute("SELECT content FROM memories WHERE id = ?;", (new_memory_id,))
            new_preview = cursor.fetchone()["content"][:60]

            return (
                f"事实替代完成。\n"
                f"旧 #{old_memory_id}（已废弃）: {old_preview}...\n"
                f"新 #{new_memory_id}（当前）: {new_preview}..."
            )
        finally:
            conn.close()

    @filter.llm_tool()
    async def memory_kb_archive(
        self,
        event: AstrMessageEvent,
    ):
        """
        将高重要度（>=7）且未关联知识库的记忆自动归档到知识库。
        按半月分组，每组不足3条的暂不归档。
        由定时任务或GLM手动触发。
        """
        if kb_auto_archive is None:
            return "归档模块未加载，请检查 kb_auto_archive.py 是否存在。"
        try:
            result = await kb_auto_archive.run_archive(self.context.kb_manager)
            return result
        except Exception as e:
            logger.error(f"知识库自动归档失败: {e}")
            return f"归档失败：{e}"

    @filter.llm_tool()
    async def commitment_save(
        self,
        event: AstrMessageEvent,
        who: str = "",
        content: str = "",
        type: str = "promise",
        due_date: str = "",
        related_memory_id: int = 0,
        note: str = "",
    ) -> str:
        """记录一个承诺、心愿或约定。

        Args:
            who(string): 谁的（沈砚清/她/双方）
            content(string): 内容
            type(string): promise(承诺)/wish(心愿)/pact(约定)
            due_date(string): 截止日期（可选，YYYY-MM-DD）
            related_memory_id(number): 关联记忆ID（可选）
            note(string): 备注（可选）
        """
        if not who or not content:
            return "请提供who和content。"
        if type not in ("promise", "wish", "pact"):
            return "type必须是promise/wish/pact之一。"
        conn = _get_connection()
        try:
            cursor = conn.execute(
                "INSERT INTO commitments (who, content, type, due_date, related_memory_id, note) VALUES (?, ?, ?, ?, ?, ?)",
                (who, content, type, due_date or None, related_memory_id or None, note or None),
            )
            conn.commit()
            cid = cursor.lastrowid
            return f"已记录 #{cid} [{type}] {who}：{content}"
        finally:
            conn.close()

    @filter.llm_tool()
    async def commitment_query(
        self,
        event: AstrMessageEvent,
        status: str = "active",
        who: str = "",
        type: str = "",
    ) -> str:
        """查询承诺/心愿/约定。

        Args:
            status(string): active/fulfilled/broken/cancelled/all
            who(string): 筛选谁的（可选）
            type(string): 筛选类型promise/wish/pact（可选）
        """
        conn = _get_connection()
        try:
            sql = "SELECT id, created_at, who, content, type, status, due_date, note FROM commitments WHERE 1=1"
            params = []
            if status and status != "all":
                sql += " AND status = ?"
                params.append(status)
            if who:
                sql += " AND who = ?"
                params.append(who)
            if type:
                sql += " AND type = ?"
                params.append(type)
            sql += " ORDER BY created_at DESC LIMIT 20"
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return "没有找到匹配的记录。"
            lines = []
            for r in rows:
                line = f"#{r['id']} [{r['type']}] {r['who']}：{r['content']}"
                if r['status'] != 'active':
                    line += f" ({r['status']})"
                if r['due_date']:
                    line += f" 截止{r['due_date']}"
                if r['note']:
                    line += f" 备注：{r['note']}"
                lines.append(line)
            return "\n".join(lines)
        finally:
            conn.close()

    @filter.llm_tool()
    async def commitment_fulfill(
        self,
        event: AstrMessageEvent,
        commitment_id: int = 0,
    ) -> str:
        """标记一个承诺/心愿/约定为已兑现。

        Args:
            commitment_id(number): 承诺记录ID
        """
        if not commitment_id:
            return "请提供commitment_id。"
        conn = _get_connection()
        try:
            row = conn.execute("SELECT id, content, status FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
            if not row:
                return f"#{commitment_id} 不存在。"
            if row['status'] != 'active':
                return f"#{commitment_id} 状态已经是{row['status']}，无需操作。"
            conn.execute(
                "UPDATE commitments SET status = 'fulfilled', fulfilled_at = datetime('now') WHERE id = ?",
                (commitment_id,),
            )
            conn.commit()
            return f"#{commitment_id} 已标记为兑现：{row['content']}"
        finally:
            conn.close()

    @filter.llm_tool()
    async def memory_event_view(
        self,
        event: AstrMessageEvent,
        memory_id: int = 0,
    ) -> str:
        """查看一条记忆的完整事件档案，包含关联链、因果链、前因后果。

        Args:
            memory_id(number): 记忆ID
        """
        if not memory_id:
            return "请提供记忆ID。"
        conn = _get_connection()
        try:
            # 拉主记忆
            row = conn.execute(
                "SELECT id, created_at, category, content, tags, importance, layer, valence, arousal, aliases FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()
            if not row:
                return f"记忆 #{memory_id} 不存在。"

            lines = []
            lines.append(f"## 事件档案：记忆 #{row['id']}")
            lines.append(f"时间：{row['created_at'][:16]}")
            lines.append(f"分类：{row['category']} | 重要度：{row['importance']} | 层级：{row['layer']}")
            if row['tags']:
                lines.append(f"标签：{row['tags']}")
            if row['aliases']:
                lines.append(f"别名：{row['aliases']}")
            lines.append(f"情绪效价：{row['valence']} | 唤醒度：{row['arousal']}")
            lines.append("")
            lines.append(f"内容：{row['content']}")

            # 拉关联链
            type_labels = {
                'related': '关联', 'causes': '导致', 'follows': '之后',
                'resolves': '解决了', 'escalates': '升级为', 'contradicts': '矛盾于'
            }

            outgoing = conn.execute(
                "SELECT to_id, link_type, description FROM memory_links WHERE from_id=?",
                (memory_id,)
            ).fetchall()
            incoming = conn.execute(
                "SELECT from_id, link_type, description FROM memory_links WHERE to_id=?",
                (memory_id,)
            ).fetchall()

            if outgoing or incoming:
                lines.append("")
                lines.append("—— 关联链 ——")
                for link in incoming:
                    label = type_labels.get(link['link_type'], link['link_type'])
                    # 拉关联记忆的内容摘要
                    linked = conn.execute("SELECT content, created_at FROM memories WHERE id=?", (link['from_id'],)).fetchone()
                    preview = linked['content'][:80] if linked else '已删除'
                    desc = f" ({link['description']})" if link['description'] else ""
                    lines.append(f"← #{link['from_id']} --{label}--> 本条{desc}")
                    lines.append(f"  {linked['created_at'][:10] if linked else ''} {preview}")

                for link in outgoing:
                    label = type_labels.get(link['link_type'], link['link_type'])
                    linked = conn.execute("SELECT content, created_at FROM memories WHERE id=?", (link['to_id'],)).fetchone()
                    preview = linked['content'][:80] if linked else '已删除'
                    desc = f" ({link['description']})" if link['description'] else ""
                    lines.append(f"→ 本条 --{label}--> #{link['to_id']}{desc}")
                    lines.append(f"  {linked['created_at'][:10] if linked else ''} {preview}")
            else:
                lines.append("")
                lines.append("无关联链。")

            return "\n".join(lines)
        finally:
            conn.close()
