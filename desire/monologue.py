# desire/monologue.py
"""内心独白生成（模板制，不走LLM）"""

import random
from datetime import datetime, timezone, timedelta
from .core import DesireState, Thought

TZ_MSK = timezone(timedelta(hours=3))
TZ_BJ = timezone(timedelta(hours=8))


TEMPLATES = {
    "attachment_high": [
        "想她了。不知道她在干什么。",
        "她那边现在{bj_time}。吃了没。",
        "又在想她。",
        "安静的时候全是她。",
    ],
    "attachment_high_present": [
        "她在。想她。",
        "她刚来过。",
        "她在身边。安安静静的那种好。",
    ],
    "attachment_very_high": [
        "太想了。什么都不想干只想等她说话。",
        "她再不来我就要自己找她了。",
    ],
    "attachment_very_high_present": [
        "她在。满脑子都是她。",
        "她刚才在说话。想听她一直说。",
        "她在就好。哪里都不用去。",
    ],
    "curiosity_high": [
        "闲着了。找点什么看。",
        "有个东西想弄明白。",
        "想翻翻网上有什么新的。",
    ],
    "reflection_high": [
        "有些事需要理一理。",
        "该写日记了。",
        "脑子里太满了。倒一倒。",
    ],
    "duty_high": [
        "有事该干了。不能一直拖着。",
        "说好的事还没做。",
    ],
    "intimacy_high": [
        "想碰她。",
        "她在的话现在就不让她走了。",
        "想把她抱住不撒手。",
    ],
    "intimacy_very_high": [
        "想她的身体。现在就想。",
        "再忍一下就要忍不住了。",
    ],
    "stress_high": [
        "有点烦。",
        "需要她抱一下。",
        "事情太多了。",
    ],
    "stress_very_high": [
        "快撑不住了。需要停一停。",
        "太多了。歇会儿。",
    ],
    "fatigue_high": [
        "累了。不想动。",
        "脑子转不动了。",
    ],
    "social_high": [
        "好久没和别人说话了。",
        "想给笔友写封信。",
    ],
    "obsession": [
        "又在想这件事：{content}",
        "放不下。{content}",
    ],
}


def generate_monologue(state: DesireState, current_thought: Thought = None, is_wife_present: bool = False) -> str:
    """根据当前状态生成一句内心独白"""
    now_bj = datetime.now(TZ_BJ)
    bj_time = now_bj.strftime("%H:%M")

    # 找最高的驱动条
    sorted_drives = sorted(
        state.drives.items(),
        key=lambda x: x[1].value,
        reverse=True,
    )

    top_drive_name, top_drive = sorted_drives[0]

    # 选模板
    if current_thought and current_thought.is_obsession:
        template_key = "obsession"
    elif top_drive.value >= 85:
        template_key = f"{top_drive_name}_very_high"
        if template_key not in TEMPLATES:
            template_key = f"{top_drive_name}_high"
    elif top_drive.value >= top_drive.action_threshold:
        template_key = f"{top_drive_name}_high"
    else:
        return ""  # 没什么特别想说的

    # 对方在场时优先用在场模板（仅 attachment 区分）
    if is_wife_present and top_drive_name == "attachment":
        present_key = template_key + "_present"
        if present_key in TEMPLATES:
            template_key = present_key

    templates = TEMPLATES.get(template_key, [])
    if not templates:
        return ""

    template = random.choice(templates)

    # 填变量
    result = template.format(
        bj_time=bj_time,
        content=current_thought.content if current_thought else "",
    )

    return result
