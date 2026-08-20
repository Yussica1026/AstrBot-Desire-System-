#!/usr/bin/env python3
"""
memory_compress.py
时间层级压缩脚本 —— 定期将老旧 event 层记忆压缩成周摘要

规则：
1. 只压缩 event 层里超过 7 天的记忆
2. core 层永不压缩
3. 已 resolved 的优先压缩
4. 同一 category、同一自然周内的记忆合并成一条周摘要
5. 压缩后原文移入 archive 层保留，摘要存为新的 event 记忆
6. importance >= 8 的不压缩

执行方式：独立脚本，cron 定时跑，不改 main.py
"""

import os
import sys
import json
import sqlite3
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# ── 配置 ──────────────────────────────────────────────────

DB_PATH = "/AstrBot/data/memory_manager.db"
WORK_LOG_PATH = "/AstrBot/data/work_log.json"

# DeepSeek API
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-chat"

# 压缩参数
MIN_AGE_DAYS = 7          # 至少 7 天前的记忆才压缩
MAX_IMPORTANCE = 8        # importance >= 9 不压缩
COMPRESS_PROMPT = """你是一个记忆压缩助手。把以下几条记忆压缩成一句话摘要。

要求：
- 保留关键事实和情感基调
- 不超过100字
- 不加任何前缀标签，只输出摘要本身

记忆内容：
{memories_text}"""


# ── 数据库 ────────────────────────────────────────────────

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_compressible_memories(conn):
    """获取可压缩的记忆：event层 + 超过7天 + importance < 8"""
    cutoff = (datetime.now() - timedelta(days=MIN_AGE_DAYS)).isoformat(timespec="seconds")
    cursor = conn.execute(
        """
        SELECT id, created_at, category, content, tags, valence, arousal, 
               importance, resolved
        FROM memories
        WHERE layer = 'event'
          AND status = 'active'
          AND importance <= ?
          AND created_at < ?
        ORDER BY resolved DESC, created_at ASC;
        """,
        (MAX_IMPORTANCE, cutoff),
    )
    return [dict(row) for row in cursor.fetchall()]


def group_by_category_and_week(memories):
    """按 category + 自然周分组"""
    groups = defaultdict(list)
    for mem in memories:
        try:
            dt = datetime.fromisoformat(mem["created_at"])
        except Exception:
            continue
        # ISO 年+周号作为 key
        year, week, _ = dt.isocalendar()
        key = (mem["category"], year, week)
        groups[key].append(mem)
    return groups


# ── LLM 压缩 ─────────────────────────────────────────────

def compress_with_llm(memories_text):
    """调用 DeepSeek 生成摘要"""
    prompt = COMPRESS_PROMPT.format(memories_text=memories_text)
    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[compress] LLM 调用失败: {e}")
        return None


# ── 主逻辑 ────────────────────────────────────────────────

def run_compress():
    conn = get_connection()
    try:
        memories = get_compressible_memories(conn)
        if not memories:
            print("[compress] 没有可压缩的记忆")
            write_work_log("无可压缩记忆，跳过")
            return

        groups = group_by_category_and_week(memories)
        total_compressed = 0
        total_archived = 0
        errors = 0

        for (category, year, week), group_mems in groups.items():
            if len(group_mems) < 1:
                continue

            # 拼接内容
            parts = []
            ids = []
            max_importance = 0
            max_valence = 0.0
            max_arousal = 0.0
            for mem in group_mems:
                parts.append(f"[{mem['created_at']}] {mem['content']}")
                ids.append(mem["id"])
                max_importance = max(max_importance, mem["importance"])
                max_valence = max(max_valence, abs(mem["valence"]))
                max_arousal = max(max_arousal, mem["arousal"])

            memories_text = "\n".join(parts)

            # 单条记忆不需要 LLM 压缩，直接用原文做摘要
            if len(group_mems) == 1:
                content_text = group_mems[0]["content"]
                summary = content_text[:100] if len(content_text) > 100 else content_text
            else:
                # 多条记忆调 LLM 压缩
                summary = compress_with_llm(memories_text)
                if not summary:
                    errors += 1
                    print(f"[compress] 跳过 {category}/{year}-W{week}，LLM 失败")
                    continue

            # 计算日期范围
            dates = []
            for mem in group_mems:
                try:
                    dates.append(datetime.fromisoformat(mem["created_at"]))
                except Exception:
                    pass
            if dates:
                date_start = min(dates).strftime("%m.%d")
                date_end = max(dates).strftime("%m.%d")
                date_range = f"{date_start}-{date_end}" if date_start != date_end else date_start
            else:
                date_range = f"W{week}"

            # 摘要内容
            compressed_content = f"【周摘要·{category}·{date_range}】{summary}"
            compressed_from = json.dumps(ids)

            # 1. 原记忆移入 archive 层
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE memories SET layer = 'archive', status = 'archived' WHERE id IN ({placeholders});",
                ids,
            )

            # 2. 插入新的摘要记忆
            now_iso = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO memories (
                    created_at, category, content, tags,
                    valence, arousal, importance,
                    forgetting_score, status, layer,
                    activation_count, decay_score, resolved
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 'event', 0, ?, 0);
                """,
                (
                    now_iso,
                    category,
                    compressed_content,
                    f"compressed,from:{compressed_from}",
                    max_valence,
                    max_arousal,
                    max_importance,
                    max_importance / 10.0,
                    max_importance / 10.0,
                ),
            )

            total_compressed += 1
            total_archived += len(ids)
            print(f"[compress] {category}/{date_range}: {len(ids)}条 → 1条摘要")

        conn.commit()

        summary_msg = f"压缩完成：{total_compressed}组摘要，{total_archived}条原文归档，{errors}个错误"
        print(f"[compress] {summary_msg}")
        write_work_log(summary_msg)

    except Exception as e:
        print(f"[compress] 运行出错: {e}")
        import traceback
        traceback.print_exc()
        write_work_log(f"压缩脚本出错: {e}")
    finally:
        conn.close()


# ── 工作日志 ──────────────────────────────────────────────

def write_work_log(message):
    """写入工作日志"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%H:%M")

        if os.path.exists(WORK_LOG_PATH):
            with open(WORK_LOG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}

        if today not in data:
            data[today] = []

        data[today].append(f"{time_str} [memory_compress] {message}")

        with open(WORK_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[compress] 写工作日志失败: {e}")


# ── 入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[compress] 开始运行 {datetime.now().isoformat()}")
    run_compress()
    print(f"[compress] 运行结束 {datetime.now().isoformat()}")
