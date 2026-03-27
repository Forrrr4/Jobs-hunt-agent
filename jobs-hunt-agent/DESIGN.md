# DESIGN.md — Job Hunt Agent 架构设计文档

> 本文档描述系统的整体设计思路、模块职责、数据流和技术决策。  
> 由项目初始化时生成，重大设计变更时需同步更新本文档。

---

## 一、设计目标

| 目标 | 说明 |
|------|------|
| 自动化程度 | 一键触发，无需人工干预（除首次登录和最终投递确认） |
| 可扩展性 | 新增平台只需实现统一接口，无需修改核心逻辑 |
| 可观测性 | 每个步骤有日志、数据库记录和进度汇报 |
| 安全性 | 简历事实不被篡改；不过度投递（每日上限） |
| 可中断恢复 | Agent 中途崩溃后可从上次状态继续 |

---

## 二、系统架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        main.py  入口                            │
│                  --mode: full / crawl-only / dry-run            │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│               Orchestrator  (LangGraph StateGraph)              │
│                                                                 │
│   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌─────────┐  │
│   │  crawl   │───▶│  filter  │───▶│  tailor  │───▶│  apply  │  │
│   │  _node   │    │  _node   │    │  _node   │    │  _node  │  │
│   └──────────┘    └──────────┘    └──────────┘    └─────────┘  │
│        │               │               │               │        │
│        │        score<65: skip         │         dry-run: skip  │
│        │               │               │               │        │
│        ▼               ▼               ▼               ▼        │
│                  AgentState（贯穿全流程的共享状态）              │
└─────────────────────────────────────────────────────────────────┘
                               │
              ┌────────────────┼─────────────────┐
              ▼                ▼                 ▼
      tools/browser.py   tools/llm_client.py   tools/db.py
      (Playwright)        (Anthropic SDK)       (SQLite)
```

---

## 三、模块详细设计

### 3.1 AgentState（共享状态）

```python
# models/agent_state.py
from typing import TypedDict, List, Optional
from models.job_posting import JobPosting

class AgentState(TypedDict):
    # 运行配置
    config: dict                          # 从 config.yaml 读取的完整配置
    run_mode: str                         # full / crawl-only / dry-run
    
    # 各阶段数据
    jobs_found: List[JobPosting]          # 爬取到的原始职位列表
    jobs_filtered: List[JobPosting]       # 筛选后（有 score）的职位列表
    jobs_to_apply: List[JobPosting]       # 达到分数线的职位（待投递）
    
    # 执行结果
    resumes_generated: List[dict]         # {job_id: ..., resume_path: ...}
    applications_sent: List[dict]         # {job_id: ..., status: ..., timestamp: ...}
    
    # 错误追踪
    errors: List[dict]                    # {module: ..., error: ..., job_id: ...}
    
    # 流程控制
    current_phase: str                    # crawl / filter / tailor / apply / done
    should_stop: bool                     # 遇到严重错误时置为 True
```

### 3.2 Crawler Module（信息采集）

**职责**：从招聘平台抓取符合条件的岗位，标准化后存入数据库。

**数据流**：
```
config.yaml（搜索条件）
    → Playwright 打开平台搜索页
    → 解析职位列表（标题、公司、薪资、地点、JD链接）
    → 点击进入 JD 详情页，抓取完整描述
    → 构造 JobPosting 对象
    → 去重检查（URL 或平台ID）
    → 写入 SQLite jobs 表
    → 更新 AgentState.jobs_found
```

**关键实现细节**：
- 使用 `tools/browser.py` 的 `human_delay(min=2, max=5)` 模拟人类操作节奏
- Cookie 持久化：每次启动检查 `.cookies/` 目录，过期则弹出提示
- 实习僧：可用公开 API + 页面解析（无需登录）
- Boss直聘：需要登录态，cookie 有效期约 7 天
- 翻页上限：由 `config.yaml` 的 `limits.max_jobs_per_run` 控制

**接口设计**：
```python
async def crawl_node(state: AgentState) -> AgentState:
    """LangGraph 节点：执行爬取，返回更新后的 state"""
    ...

class ShixisengCrawler:
    async def fetch_job_list(self, keywords: list, city: str, page: int) -> List[JobPosting]: ...
    async def fetch_job_detail(self, job_id: str, url: str) -> str: ...  # 返回 JD 全文

class BossCrawler:
    async def ensure_logged_in(self) -> bool: ...
    async def fetch_job_list(self, keywords: list, city: str, page: int) -> List[JobPosting]: ...
```

**异常处理**：
- 网络超时：重试 3 次，间隔指数退避
- 登录失效：写入 errors，跳过该平台，继续其他平台
- 解析失败：记录 URL 到错误日志，跳过该条目

---

### 3.3 Filter Module（智能筛选）

**职责**：用 LLM 对每个 JD 打分，过滤出与用户匹配的岗位。

**数据流**：
```
AgentState.jobs_found（原始职位列表）
    → 批量构造 filter_prompt（JD文本 + 用户偏好）
    → 调用 Claude API（并发控制，最多 5 个并发）
    → 解析 JSON 响应（score + reason + match_points）
    → 更新 jobs 表中的 score / score_reason 字段
    → 筛选 score >= threshold 的岗位
    → 更新 AgentState.jobs_filtered
```

**Prompt 设计原则**（见 `prompts/filter_prompt.py`）：
```
系统角色：资深 HR，熟悉技术岗位招聘
评估维度：
  - 技能匹配度（40分）：JD 要求的技能与用户技能的重叠程度
  - 发展潜力（30分）：公司规模、行业地位、岗位成长空间
  - 综合条件（30分）：薪资、地点、工作时间是否符合用户要求
输出格式：严格 JSON，不得有多余文字
  {
    "score": 78,
    "reason": "...",
    "match_points": ["Python匹配", "AI方向一致"],
    "concern_points": ["要求5年经验，候选人可能不足"]
  }
```

**并发控制**：
```python
# 使用 asyncio.Semaphore 限制并发，避免触发 API 速率限制
semaphore = asyncio.Semaphore(5)
tasks = [score_job(job, semaphore) for job in jobs]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

---

### 3.4 Resume Tailor Module（简历定制）

**职责**：针对每个目标岗位，在不改变事实的前提下优化简历表述。

**数据流**：
```
AgentState.jobs_filtered（筛选后职位）
    → 读取 data/base_resume.md（基础简历）
    → 解析 JD 关键词和要求重点
    → 构造 tailor_prompt
    → LLM 返回优化建议（Diff 格式，只改措辞不改事实）
    → 应用修改，生成定制简历 Markdown
    → 转换为 PDF（WeasyPrint）
    → 保存到 data/outputs/{job_id}_{date}.pdf
    → 更新 AgentState.resumes_generated
```

**简历安全约束（核心）**：

Prompt 中必须包含以下硬性约束：
```
【严格禁止修改的内容】
- 任何公司名称、职位名称、工作时间段
- 学历、学校、专业、毕业年份
- 项目名称、技术栈（只能添加同义词，不能替换）
- 任何量化数据（如"提升30%性能"）

【允许修改的内容】
- 项目描述的表述方式（使用 JD 中出现的关键词重新表述）
- 技能列表的排序（将与 JD 匹配的技能排在前面）
- 个人简介段落（重写以突出与目标岗位的契合点）

输出格式：返回完整的修改后简历 Markdown，不需要解释。
```

**质量验证**（自动检查）：
```python
def validate_resume_integrity(original: str, tailored: str) -> bool:
    """验证简历关键信息未被篡改"""
    # 提取公司名、日期、数字，对比前后一致性
    ...
```

---

### 3.5 Applicator Module（自动投递）

**职责**：将定制简历投递到对应平台的目标岗位。

**两种模式**：

```
半自动模式（默认）：
    生成投递摘要（岗位、公司、简历路径）
    → 控制台输出，等待用户输入 Y/N
    → 确认后执行投递

全自动模式（--mode full）：
    直接执行，不等待确认
    → 投递后发送桌面通知
```

**防重复投递**：
```python
async def is_already_applied(job_id: str) -> bool:
    """检查数据库中是否已有该岗位的投递记录"""
    ...
```

**投递记录**：
每次投递后截图（Playwright screenshot），存入 `data/screenshots/`，记录到 applications 表。

---

### 3.6 Orchestrator（LangGraph 工作流）

**状态图设计**：

```
START
  │
  ▼
crawl_node ──[成功]──▶ filter_node ──[有匹配岗位]──▶ tailor_node ──[简历生成完成]──▶ apply_node ──▶ END
    │                      │                              │                              │
  [失败]              [无匹配岗位]                      [失败]                       [全部投递完成]
    │                      │                              │
    ▼                      ▼                              ▼
 error_node ──────────── END                          error_node
```

**状态持久化**（支持中断恢复）：
```python
from langgraph.checkpoint.sqlite import SqliteSaver

# 使用 SQLite 保存 checkpoint
checkpointer = SqliteSaver.from_conn_string("data/jobs.db")
graph = workflow.compile(checkpointer=checkpointer)

# 每次运行使用唯一 thread_id，支持恢复
config = {"configurable": {"thread_id": f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"}}
```

---

## 四、数据模型

### JobPosting

```python
# models/job_posting.py
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class JobPosting(BaseModel):
    id: str                          # f"{platform}_{platform_job_id}"
    title: str
    company: str
    location: str
    salary_range: Optional[str]
    jd_text: str                     # 完整 JD 文本
    platform: str                    # "shixiseng" | "boss" | "linkedin"
    url: str
    crawled_at: datetime
    
    # 筛选后填入
    score: Optional[float] = None
    score_reason: Optional[str] = None
    match_points: Optional[list] = None
    
    # 状态
    status: str = "new"              # new → filtered → tailored → applied → rejected
```

---

## 五、技术选型决策记录

| 决策 | 选择 | 备选方案 | 选择理由 |
|------|------|---------|---------|
| 工作流编排 | LangGraph | Celery, Prefect | 天然支持 LLM Agent 模式；有状态图；支持 checkpoint |
| 网页抓取 | Playwright | Selenium, Scrapy | 更好的反爬处理；原生异步支持；截图功能 |
| LLM | Claude claude-sonnet-4-20250514 | GPT-4, Gemini | 函数调用能力强；长上下文处理简历质量更高 |
| 数据库 | SQLite | PostgreSQL, MongoDB | 零配置；足够轻量；便于分发 |
| 简历格式 | Markdown → PDF | DOCX | Markdown 易于 LLM 处理；WeasyPrint 渲染质量好 |
| 配置管理 | Pydantic + YAML | .env, TOML | 类型安全；可嵌套；可读性好 |

---

## 六、关键难点与解决方案

### 难点1：平台反爬

**问题**：Boss直聘、实习僧对自动化访问有检测（请求频率、User-Agent、行为特征）

**解决方案**：
- 使用 `playwright-stealth` 插件绕过常见指纹检测
- `human_delay(2, 5)` 在每次页面操作前等待随机时间
- 使用真实 Chrome 浏览器实例（非无头模式，可选）
- Cookie 持久化，避免频繁登录触发风控

### 难点2：简历定制质量

**问题**：LLM 可能过度修改简历，改变事实内容（如夸大工作经历）

**解决方案**：
- Prompt 中明确列出"禁止修改"和"允许修改"的内容类型
- 实现 `validate_resume_integrity()` 自动检验事实一致性
- 输出 Diff 而非完整简历（LLM 只返回修改建议，程序应用修改）

### 难点3：投递准确性

**问题**：自动填表可能填错字段，或遇到平台改版导致选择器失效

**解决方案**：
- 每次投递前截图存证
- 半自动模式默认启用，人工做最后确认
- 选择器维护在独立配置文件中（`tools/platform_selectors.yaml`），便于更新

### 难点4：LLM 并发与成本

**问题**：大量 JD 同时评分可能触发 API 限速，且成本较高

**解决方案**：
- `asyncio.Semaphore(5)` 限制并发数
- 对同一公司的多个相似岗位只评分一次（去重 by company+title）
- 使用 `claude-haiku` 做初步过滤（低成本），再用 `claude-sonnet` 精细评分

---

## 七、扩展计划（Future Work）

- [ ] 支持 LinkedIn 平台（需 OAuth 登录）
- [ ] 添加 Web UI 界面（FastAPI + 简单前端，可视化投递进度）
- [ ] 投递结果追踪（HR 回复通知、面试安排提醒）
- [ ] 多用户支持（支持多套简历和配置）
- [ ] 简历质量评分（不只是匹配度，还评估简历自身质量）

---

## 八、版本历史

| 版本 | 日期 | 变更说明 |
|------|------|---------|
| v0.1 | 初始化 | 创建架构设计文档，定义模块接口和数据模型 |
