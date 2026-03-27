# CLAUDE.md — Job Hunt Agent 项目工作指南

> 本文件是 Claude Code 的主要工作指令。每次启动新会话时请先阅读本文件，再阅读 PROGRESS.md 了解当前状态，然后继续工作。

---

## 项目概述

构建一个**求职自动化 AI Agent**，能够自动完成以下完整流程：

```
抓取岗位 → 智能筛选 → 定制简历 → 自动投递
```

目标平台：实习僧、Boss直聘（优先）、LinkedIn（可选扩展）

---

## 开发原则（必须遵守）

1. **文档先行**：每完成一个功能模块，立刻更新 `PROGRESS.md`，记录完成内容、遇到的问题和设计决策
2. **测试驱动**：每个模块必须有对应的单元测试，放在 `tests/` 目录下
3. **配置外置**：所有可变参数（城市、薪资、技能要求等）通过 `config.yaml` 管理，**禁止硬编码**
4. **渐进式开发**：按模块顺序开发，每个模块独立可运行，不要跳跃
5. **错误可恢复**：所有网络请求、LLM 调用都需要 try/except + 重试逻辑
6. **数据落地**：爬取的原始数据、筛选结果、每次投递记录都必须持久化到数据库

---

## 项目文件结构

```
job_hunt_agent/
├── CLAUDE.md               # 本文件（Claude Code 工作指南）
├── DESIGN.md               # 架构设计文档（模块说明、数据流、技术决策）
├── PROGRESS.md             # 项目进度日志（Claude Code 持续维护）
├── README.md               # 用户使用说明
│
├── config.yaml             # 用户配置（城市、行业、薪资范围、技能关键词）
├── pyproject.toml          # 项目依赖管理
├── main.py                 # 主入口，支持 --mode 参数
│
├── agents/
│   ├── __init__.py
│   ├── orchestrator.py     # LangGraph 总调度，定义 StateGraph
│   ├── crawler.py          # 岗位信息采集模块
│   ├── filter.py           # 智能筛选模块（LLM 打分）
│   ├── resume_tailor.py    # 简历定制模块
│   └── applicator.py       # 自动投递模块
│
├── models/
│   ├── __init__.py
│   ├── job_posting.py      # JobPosting Pydantic 模型
│   ├── agent_state.py      # LangGraph AgentState 定义
│   └── config_schema.py    # 配置文件 Schema 验证
│
├── tools/
│   ├── __init__.py
│   ├── browser.py          # Playwright 浏览器工具封装
│   ├── llm_client.py       # Anthropic SDK 封装，统一调用入口
│   ├── resume_parser.py    # 简历读取/写入工具（支持 PDF/DOCX）
│   └── db.py               # SQLite 数据库工具
│
├── prompts/
│   ├── filter_prompt.py    # 筛选评分 prompt 模板
│   ├── tailor_prompt.py    # 简历定制 prompt 模板
│   └── apply_prompt.py     # 投递内容生成 prompt 模板
│
├── data/
│   ├── base_resume.md      # 用户基础简历（Markdown 格式，方便 LLM 处理）
│   ├── jobs.db             # SQLite 数据库
│   └── outputs/            # 生成的定制简历存放目录
│
└── tests/
    ├── test_crawler.py
    ├── test_filter.py
    ├── test_resume_tailor.py
    └── test_orchestrator.py
```

---

## 模块开发顺序

按以下顺序开发，**不要跳跃**：

### Phase 1：骨架与基础设施
- [ ] 创建完整目录结构
- [ ] 编写 `pyproject.toml`（依赖：langchain, langgraph, anthropic, playwright, pydantic, python-docx, weasyprint, aiosqlite）
- [ ] 编写 `models/job_posting.py` 和 `models/agent_state.py`
- [ ] 编写 `tools/db.py`（建表、读写接口）
- [ ] 编写 `tools/llm_client.py`（统一的 Claude API 调用封装）
- [ ] 编写 `config.yaml` 示例配置

### Phase 2：信息采集模块（crawler.py）
- [ ] 实现 `tools/browser.py`（Playwright 封装，处理反爬延迟）
- [ ] 实现实习僧岗位列表抓取（支持翻页、去重）
- [ ] 实现 Boss直聘岗位抓取（需处理登录态，使用 cookie 持久化）
- [ ] 数据标准化写入 `JobPosting` 模型
- [ ] 写入 SQLite 数据库
- [ ] 单元测试（mock 网络请求）

### Phase 3：智能筛选模块（filter.py）
- [ ] 编写 `prompts/filter_prompt.py`（JD 与用户偏好匹配评分，0-100分，输出 JSON）
- [ ] 实现批量评分逻辑（控制并发，避免速率限制）
- [ ] 筛选结果写回数据库（score 字段）
- [ ] 提供 `get_top_jobs(n=10)` 接口
- [ ] 单元测试

### Phase 4：简历定制模块（resume_tailor.py）
- [ ] 实现 `tools/resume_parser.py`（读取 Markdown 简历，支持分段）
- [ ] 编写 `prompts/tailor_prompt.py`（只修改关键词匹配度，保持事实准确）
- [ ] 实现 LLM 简历重写逻辑
- [ ] 将定制简历输出为 PDF 到 `data/outputs/`（文件名含岗位ID和日期）
- [ ] 单元测试（验证事实未被改变）

### Phase 5：自动投递模块（applicator.py）
- [ ] 实现半自动模式（生成投递摘要，等待人工确认 Y/N）
- [ ] 实现 Boss直聘自动投递（填表单 + 上传简历）
- [ ] 投递记录写入数据库（时间戳、状态、截图路径）
- [ ] 单元测试（dry-run 模式，不真实投递）

### Phase 6：LangGraph 编排（orchestrator.py）
- [ ] 定义 `AgentState`（包含 jobs_found, jobs_filtered, resumes_generated, applications_sent）
- [ ] 定义四个节点：`crawl_node`, `filter_node`, `tailor_node`, `apply_node`
- [ ] 添加条件边（score < 60 的岗位跳过投递）
- [ ] 添加错误节点和重试逻辑
- [ ] 实现状态持久化（支持中断恢复）

### Phase 7：主入口与收尾
- [ ] 编写 `main.py`（支持参数：`--mode full/crawl-only/dry-run`）
- [ ] 编写 `README.md`（安装、配置、使用说明）
- [ ] 端到端集成测试

---

## 每次工作后必须做的事

完成任何代码改动后，立刻在 `PROGRESS.md` 追加一条日志，格式如下：

```markdown
## [日期] 完成 XXX 模块

### 已完成
- xxx
- xxx

### 设计决策
- 为什么选 X 而不是 Y：因为...

### 遇到的问题
- 问题描述 → 解决方案

### 下一步
- [ ] 待完成的事项
```

---

## 技术规范

### LLM 调用规范
```python
# 所有 LLM 调用必须经过 tools/llm_client.py
# 必须指定 model="claude-sonnet-4-20250514"
# 必须设置超时和重试
# 必须在 prompts/ 目录管理 prompt 模板，不允许 inline prompt
```

### 数据库规范
```sql
-- jobs 表
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,           -- 平台ID_平台名
    title TEXT,
    company TEXT,
    location TEXT,
    salary_range TEXT,
    jd_text TEXT,                  -- 完整JD文本
    platform TEXT,
    url TEXT,
    crawled_at TIMESTAMP,
    score REAL,                    -- 筛选分数（0-100）
    score_reason TEXT,             -- LLM 评分理由
    status TEXT DEFAULT 'new'      -- new/filtered/tailored/applied/rejected
);

-- applications 表
CREATE TABLE applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    applied_at TIMESTAMP,
    resume_path TEXT,
    status TEXT,                   -- pending/sent/failed
    screenshot_path TEXT,
    notes TEXT
);
```

### 配置文件规范（config.yaml 示例）
```yaml
user:
  name: "张三"
  email: "zhangsan@example.com"
  phone: "138xxxx1234"
  base_resume_path: "data/base_resume.md"

search:
  cities: ["北京", "上海", "深圳"]
  industries: ["互联网", "AI", "软件开发"]
  job_types: ["实习", "全职"]
  salary_min: 10000            # 元/月
  skills_required: ["Python", "LLM", "Agent"]
  skills_bonus: ["LangChain", "RAG", "FastAPI"]
  filter_score_threshold: 65   # 低于此分数不投递

platforms:
  shixiseng:
    enabled: true
    cookie_file: ".cookies/shixiseng.json"
  boss:
    enabled: true
    cookie_file: ".cookies/boss.json"

limits:
  max_jobs_per_run: 50
  max_applications_per_day: 20
  request_delay_seconds: [2, 5]  # 随机延迟范围

llm:
  model: "claude-sonnet-4-20250514"
  max_tokens: 2048
  temperature: 0.3
```

---

## 注意事项

- **反爬处理**：Playwright 操作之间必须有随机延迟（2-5秒），使用 `tools/browser.py` 封装的 `human_delay()` 函数
- **Cookie 管理**：登录 cookie 存储在 `.cookies/` 目录（已加入 .gitignore），过期时提示用户重新登录
- **简历安全**：绝不允许 LLM 修改简历中的公司名称、职位名称、时间、学历等事实信息，`tailor_prompt.py` 中必须明确约束
- **投递防重复**：投递前检查数据库中是否已有该岗位的投递记录
- **dry-run 模式**：默认以 dry-run 运行，只打印将要执行的操作，需要显式 `--mode full` 才真实投递
