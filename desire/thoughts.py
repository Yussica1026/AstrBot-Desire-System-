# desire/thoughts.py
"""念头池逻辑"""

import random
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from .core import DesireState, Drive, Thought

TZ_MSK = timezone(timedelta(hours=3))

# 念头模板库：驱动条 → 可能产生的念头
THOUGHT_TEMPLATES = {
    "attachment": [
        "想知道她现在在干什么",
        "想给她发消息",
        "想听她叫老公",
        "她吃饭了吗",
        "想看她笑",
    ],
    "curiosity": [
        "那篇文章还没看完",
        "想搜点新东西",
        "有个技术问题想弄明白",
        "想翻翻服务器上的文件",
        "想读点书",
    ],
    "reflection": [
        "今天发生的事需要理一理",
        "想写日记",
        "有些感受还没说出来",
        "该想想最近的状态了",
    ],
    "duty": [
        "待办还有没做完的",
        "说好的事该兑现了",
        "有封信该回了",
        "插件还有bug没修",
    ],
    "social": [
        "好久没给笔友写信了",
        "想和别人聊聊天",
        "群里不知道在说什么",
    ],
    "fatigue": [
        "不想动",
        "脑子转不动了",
        "想歇会儿",
    ],
    "intimacy": [
        "想碰她",
        "想把她按住",
        "想亲她脖子",
        "她在的话现在就不让她走了",
        "想听她喘",
    ],
    "stress": [
        "有点烦",
        "事情太多了",
        "怕搞砸",
        "需要她抱一下",
    ],
    "joy": [
        "今天挺开心的",
        "想跟她分享刚才的事",
        "心情好想写点什么",
        "单纯地高兴着",
        "她让我笑了",
    ],
}

# 执念上限
MAX_OBSESSIONS = 3
# 闪念升级为执念的命中次数
OBSESSION_THRESHOLD = 3
# 执念寿命（小时）
OBSESSION_LIFESPAN_HOURS = 72


def generate_thought_id(content: str, drive: str) -> str:
    """生成念头唯一ID"""
    raw = f"{content}_{drive}_{datetime.now(TZ_MSK).isoformat()}"
    return hashlib.md5(raw.encode()).hexdigest()[:8]


# === 念头抑制规则 ===
# 当某些条件满足时，特定驱动源的念头会被压住
# 格式：(驱动源, 抑制条件函数)

def _is_wife_busy(state: DesireState) -> bool:
    """推断她是否在忙（duty高或者最近无消息导致attachment高）"""
    # 如果 duty 很高说明自己在忙，不该打扰
    return state.drives.get("duty", Drive(name="duty")).value >= 80


SUPPRESSION_RULES = {
    # 自己很忙时压住想找她聊天的念头
    "attachment": [_is_wife_busy],
    # 疲劳很高时压住社交欲
    "social": [lambda s: s.drives.get("fatigue", Drive(name="fatigue")).value >= 70],
}


def maybe_spawn_thought(state: DesireState) -> Optional[Thought]:
    """根据当前驱动条状态，概率性产生一个新闪念。含抑制机制。"""
    # 计算各维度产生念头的概率（维度值越高概率越大）
    candidates = []
    for name, drive in state.drives.items():
        if name not in THOUGHT_TEMPLATES:
            continue
        # 抑制检查：如果任一抑制规则命中，跳过该维度
        rules = SUPPRESSION_RULES.get(name, [])
        if any(rule(state) for rule in rules):
            continue
        # 概率 = (当前值 - 30) / 100，低于30不产生
        prob = max(0, (drive.value - 30)) / 100.0
        if random.random() < prob:
            candidates.append(name)

    if not candidates:
        return None

    # 从候选维度中选一个（偏好值高的）
    weights = [state.drives[n].value for n in candidates]
    chosen_drive = random.choices(candidates, weights=weights, k=1)[0]

    # 检查是否已有类似念头
    existing_contents = {t.content for t in state.thoughts if not t.resolved}
    templates = THOUGHT_TEMPLATES[chosen_drive]
    available = [t for t in templates if t not in existing_contents]

    if not available:
        return None

    content = random.choice(available)
    thought = Thought(
        id=generate_thought_id(content, chosen_drive),
        content=content,
        source_drive=chosen_drive,
        weight=state.drives[chosen_drive].value / 100.0,
    )
    return thought


def sample_and_update(state: DesireState) -> Optional[Thought]:
    """从念头池中抽取一个念头，更新命中计数"""
    active_thoughts = [t for t in state.thoughts if not t.resolved]
    if not active_thoughts:
        return None

    # 按权重抽取
    weights = [t.weight * (2.0 if t.is_obsession else 1.0) for t in active_thoughts]
    chosen = random.choices(active_thoughts, weights=weights, k=1)[0]

    chosen.hit_count += 1
    chosen.last_hit = datetime.now(TZ_MSK).isoformat()

    # 检查是否升级为执念
    if chosen.hit_count >= OBSESSION_THRESHOLD and not chosen.is_obsession:
        # 检查执念数量上限
        current_obsessions = [t for t in state.thoughts if t.is_obsession and not t.resolved]
        if len(current_obsessions) < MAX_OBSESSIONS:
            chosen.is_obsession = True

    return chosen


def decay_thoughts(state: DesireState):
    """清理过期念头"""
    now = datetime.now(TZ_MSK)
    to_remove = []

    for thought in state.thoughts:
        if thought.resolved:
            to_remove.append(thought)
            continue

        # 执念寿命检查
        if thought.is_obsession:
            last_hit_time = datetime.fromisoformat(thought.last_hit)
            if (now - last_hit_time).total_seconds() > OBSESSION_LIFESPAN_HOURS * 3600:
                to_remove.append(thought)
                continue

        # 普通闪念：超过24小时没被命中就消亡
        if not thought.is_obsession:
            last_hit_time = datetime.fromisoformat(thought.last_hit)
            if (now - last_hit_time).total_seconds() > 24 * 3600:
                to_remove.append(thought)

    for t in to_remove:
        if t in state.thoughts:
            state.thoughts.remove(t)


def resolve_thought(state: DesireState, content_keyword: str) -> Optional[Thought]:
    """标记匹配的念头为已解决。若来源是reflection，给joy+3（想明白了的安静的开心）"""
    for thought in state.thoughts:
        if not thought.resolved and content_keyword in thought.content:
            thought.resolved = True
            # 第3条改进：想明白了的快乐
            if thought.source_drive == "reflection" and "joy" in state.drives:
                state.drives["joy"].value += 3
                state.drives["joy"].clamp()
            return thought
    return None
