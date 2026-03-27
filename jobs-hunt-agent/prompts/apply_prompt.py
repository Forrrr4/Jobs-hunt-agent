"""
投递打招呼消息 Prompt 模板。

Boss直聘投递本质是「发送第一条消息」开启对话。
消息需要：简短（50-120字）、具体（呼应 JD 关键词）、自然（避免套话）。
"""

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 系统提示词
# ---------------------------------------------------------------------------

APPLY_SYSTEM_PROMPT = """\
你是一名正在求职的候选人，通过 Boss直聘 向招聘方发送第一条打招呼消息。

## 要求

1. **字数**：50-120 字，简洁有力
2. **结构**：
   - 开头一句：表明应聘意向 + 职位名称
   - 中间 1-2 句：结合 JD 列出最匹配的 1-2 个技能/项目亮点，要具体
   - 结尾一句：表达期待进一步沟通
3. **语气**：自信、专业，不卑不亢
4. **禁止**：
   - 禁止以「您好」「尊敬的」等通用敬语开头
   - 禁止使用与 JD 无关的技能描述
   - 禁止使用明显的模板感语句（如「期待您的回复」「非常荣幸」）

## 输出

直接输出消息正文，不要加引号、标签或任何解释性文字。
"""


# ---------------------------------------------------------------------------
# 用户消息模板
# ---------------------------------------------------------------------------

APPLY_USER_TEMPLATE = """\
## 目标职位

- **职位名称**：{job_title}
- **公司名称**：{job_company}
- **工作地点**：{job_location}

## 职位描述摘要（前 400 字）

{jd_summary}

## 我的信息

- 核心技能：{skills}
- 与该职位的匹配亮点：{match_points}

请生成一条针对该职位的打招呼消息。
"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class ApplyInput:
    """封装单次投递打招呼消息生成所需的全部输入。"""
    job_title: str
    job_company: str
    job_location: str
    jd_text: str                       # 完整 JD，内部截断为摘要
    user_name: str
    skills: list[str] = field(default_factory=list)
    match_points: list[str] = field(default_factory=list)


def build_apply_prompt(inp: ApplyInput) -> str:
    """构造发送给 LLM 的用户消息。"""
    # JD 只取前 400 字，节省 token
    jd_summary = inp.jd_text.strip()[:400]
    if len(inp.jd_text.strip()) > 400:
        jd_summary += "..."

    skills_str = "、".join(inp.skills) if inp.skills else "见简历"
    match_str = "；".join(inp.match_points) if inp.match_points else "技能与 JD 高度匹配"

    return APPLY_USER_TEMPLATE.format(
        job_title=inp.job_title,
        job_company=inp.job_company,
        job_location=inp.job_location or "未知",
        jd_summary=jd_summary,
        skills=skills_str,
        match_points=match_str,
    )
