"""
智能筛选模块的 Prompt 模板。

评分维度（共 100 分）：
  - 技能匹配度（40 分）：JD 要求与用户技能的重叠程度
  - 发展潜力（30 分）：公司规模/行业地位/岗位成长空间
  - 综合条件（30 分）：薪资/地点/工作时间与用户需求的契合度

输出：严格 JSON，不允许有任何额外文字。
"""

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# 系统提示词（固定）
# ---------------------------------------------------------------------------

FILTER_SYSTEM_PROMPT = """\
你是一名资深职业顾问，擅长技术岗位的岗位匹配分析。
你将收到一份职位描述（JD）和求职者的偏好配置，需要客观、精准地评估该职位与求职者的匹配程度。

## 评分规则

请从以下三个维度打分，总分 100 分：

### 1. 技能匹配度（满分 40 分）
- 35-40 分：JD 要求的核心技能与求职者技能高度重叠（≥80%）
- 25-34 分：部分匹配（50%-80%），求职者能快速上手
- 15-24 分：有一定相关性，但有明显技能缺口
- 0-14 分：技能方向差异较大，基本不匹配

### 2. 发展潜力（满分 30 分）
- 25-30 分：知名企业/独角兽/头部创业公司，岗位技术含量高，有清晰晋升路径
- 18-24 分：中型正规企业或有潜力的早期创业公司，岗位有一定成长空间
- 10-17 分：一般规模企业，岗位偏执行，成长空间有限
- 0-9 分：公司信息模糊，岗位重复性高，成长价值低

### 3. 综合条件（满分 30 分）
- 25-30 分：薪资范围合理、地点匹配、工作时间弹性
- 18-24 分：大部分条件符合，个别方面有折扣
- 10-17 分：薪资偏低或地点不便，但其他方面尚可
- 0-9 分：薪资不符合预期，或地点不在目标城市，或工作量严重超标

## 输出格式

严格返回如下 JSON，不得有任何额外说明、markdown 代码块或其他文字：

{
  "score": <0-100 的整数>,
  "reason": "<2-4句话的综合评价，说明打分依据>",
  "match_points": ["<匹配优势1>", "<匹配优势2>"],
  "concern_points": ["<关注点/风险1>", "<关注点/风险2>"]
}
"""


# ---------------------------------------------------------------------------
# 用户消息模板
# ---------------------------------------------------------------------------

FILTER_USER_TEMPLATE = """\
## 求职者偏好

- **目标城市**：{cities}
- **必备技能**：{skills_required}
- **加分技能**：{skills_bonus}
- **最低薪资**：{salary_min}
- **目标行业**：{industries}
- **岗位类型**：{job_types}

---

## 职位信息

- **职位名称**：{title}
- **公司名称**：{company}
- **工作地点**：{location}
- **薪资范围**：{salary_range}
- **职位来源**：{platform}

### 职位描述（JD）

{jd_text}

---

请根据上述信息，按评分规则给出评分和分析。
"""


# ---------------------------------------------------------------------------
# 数据类：封装单次评分的输入参数
# ---------------------------------------------------------------------------

@dataclass
class FilterInput:
    """封装一次 LLM 筛选评分所需的全部输入。"""
    # 职位信息
    job_id: str
    title: str
    company: str
    location: str
    salary_range: str
    platform: str
    jd_text: str

    # 用户偏好（从 config.yaml 读取）
    cities: list[str]
    skills_required: list[str]
    skills_bonus: list[str]
    salary_min: int
    industries: list[str]
    job_types: list[str]


def build_filter_prompt(inp: FilterInput) -> str:
    """
    根据 FilterInput 构造发送给 LLM 的用户消息。

    Returns:
        填充后的用户消息字符串（与 FILTER_SYSTEM_PROMPT 配合使用）
    """
    return FILTER_USER_TEMPLATE.format(
        # 用户偏好
        cities="、".join(inp.cities) if inp.cities else "不限",
        skills_required="、".join(inp.skills_required) if inp.skills_required else "不限",
        skills_bonus="、".join(inp.skills_bonus) if inp.skills_bonus else "无",
        salary_min=f"{inp.salary_min} 元/月" if inp.salary_min else "不限",
        industries="、".join(inp.industries) if inp.industries else "不限",
        job_types="、".join(inp.job_types) if inp.job_types else "不限",
        # 职位信息
        title=inp.title,
        company=inp.company,
        location=inp.location or "未知",
        salary_range=inp.salary_range or "面议",
        platform=inp.platform,
        jd_text=inp.jd_text.strip(),
    )
