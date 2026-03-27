# PROGRESS.md — 项目进度日志

---

## 项目状态总览

| 模块 | 状态 | 备注 |
|------|------|------|
| 项目骨架 | ✅ 已完成 | 目录结构、文档已就绪 |
| Phase 1: 基础设施 | ✅ 已完成 | 2026-03-27 |
| Phase 2: Crawler | ✅ 已完成 | 2026-03-27 |
| Phase 3: Filter | 🔄 待 API key 完成验证 | 2026-03-27 代码已完成 |
| Phase 4: Resume Tailor | ✅ 已完成 | 2026-03-27 |
| Phase 5: Applicator | ✅ 已完成 | 2026-03-27 |
| Phase 6: Orchestrator | ✅ 已完成 | 2026-03-27 |
| Phase 7: 收尾 | ✅ 已完成 | 2026-03-27 |

---

## 日志

## [初始化] 项目文档创建

### 已完成
- 创建 `CLAUDE.md`：Claude Code 工作指南，定义文件结构、开发顺序、技术规范
- 创建 `DESIGN.md`：完整架构设计文档，包含模块设计、数据流、技术选型决策
- 创建 `PROGRESS.md`：本文件，项目进度追踪

### 设计决策
- 选择 LangGraph 作为工作流引擎：原生支持 LLM Agent 模式，checkpoint 机制支持中断恢复
- 选择 SQLite 而非 PostgreSQL：零配置，适合单机部署，足够轻量
- 默认启用半自动模式（投递需人工确认）：保证投递质量，避免误投

### 下一步
- [ ] 开始 Phase 1：创建目录结构、编写 pyproject.toml、实现基础模型和数据库工具

---

## [2026-03-27] 完成 Phase 1：基础设施

### 已完成

**目录结构**
- 创建所有模块目录：`agents/`、`models/`、`tools/`、`prompts/`、`tests/`、`data/outputs/`、`data/screenshots/`、`.cookies/`
- 各目录均含 `__init__.py`（Python 包）

**pyproject.toml**
- 依赖：`langgraph`, `langchain`, `langchain-anthropic`, `anthropic`, `playwright`, `pydantic`, `pydantic-settings`, `pyyaml`, `aiosqlite`, `python-docx`, `weasyprint`, `markdown`, `tenacity`, `rich`
- 开发依赖：`pytest`, `pytest-asyncio`, `pytest-mock`, `ruff`
- 配置 pytest 默认异步模式

**models/job_posting.py**
- `JobPosting` Pydantic v2 模型
- 字段：id、title、company、location、salary_range、jd_text、platform、url、crawled_at
- 筛选后字段：score（0-100）、score_reason、match_points、concern_points
- 状态字段：status（new → filtered → tailored → applied → rejected）

**models/agent_state.py**
- `AgentState` TypedDict，定义 LangGraph 共享状态
- 包含：config、run_mode、jobs_found、jobs_filtered、jobs_to_apply、resumes_generated、applications_sent、errors、current_phase、should_stop
- `make_initial_state()` 工厂函数

**models/config_schema.py**
- Pydantic v2 配置 Schema：`AppConfig`、`UserConfig`、`SearchConfig`、`PlatformsConfig`、`LimitsConfig`、`LLMConfig`

**tools/db.py**
- 异步 SQLite 封装（`aiosqlite`）
- `init_db()`：幂等建表（jobs、applications）
- jobs 表操作：`upsert_job()`、`update_job_score()`、`update_job_status()`、`get_job_by_id()`、`get_jobs_by_status()`、`get_top_jobs(n, min_score)`、`job_exists()`
- applications 表操作：`insert_application()`、`is_already_applied()`、`update_application_status()`
- WAL 模式开启，支持并发读写

**tools/llm_client.py**
- 全局 `AsyncAnthropic` 客户端懒加载（单例）
- `call_llm()`：带 tenacity 重试（RateLimitError、APIConnectionError，最多 3 次，指数退避）
- `call_llm_json()`：自动剥离 markdown 代码块，解析 JSON，支持 fallback
- `call_llm_fast()` / `call_llm_fast_json()`：使用 claude-haiku 的轻量级调用

**config.yaml**
- 完整示例配置，涵盖 user、search、platforms、limits、llm 所有字段
- `data/base_resume.md`：简历模板占位文件

**其他**
- `.gitignore`：排除 `.cookies/`、`data/jobs.db`、`data/outputs/`、`data/screenshots/`、`.env`

### 设计决策

- **tenacity 重试策略**：仅对 `RateLimitError` 和 `APIConnectionError` 重试（网络抖动），`AuthenticationError` 等立即抛出，避免无效重试消耗配额
- **JSON 解析健壮性**：`call_llm_json()` 主动剥离 ` ```json ``` ` 包裹，减少 prompt 工程负担
- **haiku 分层调用**：`call_llm_fast*()` 系列使用 claude-haiku，成本约为 sonnet 的 1/10，适合批量初步筛选
- **WAL 模式**：数据库开启 WAL，支持爬虫写入和筛选读取的并发操作

### 遇到的问题
- 无（Phase 1 为纯基础设施，无网络/LLM 依赖）

### 下一步（已完成）
- [x] Phase 2：实现 `tools/browser.py`（Playwright 封装）
- [x] Phase 2：实现 `agents/crawler.py`（实习僧 + Boss直聘抓取）
- [x] Phase 2：编写 `tests/test_crawler.py`（mock 网络请求）

---

## [2026-03-27] 完成 Phase 2：Crawler 模块

### 已完成

**tools/browser.py**
- `BrowserManager`：完整的 Playwright 生命周期管理（`async with` 支持）
- Chromium 启动参数：`--disable-blink-features=AutomationControlled` 等反检测配置
- `_STEALTH_SCRIPT`：页面注入脚本，移除 `navigator.webdriver`、模拟真实插件和语言配置
- `human_delay(min, max)`：随机延迟函数，在页面操作间调用
- `goto_with_retry(page, url, retries=3)`：带指数退避重试的页面跳转（2s/4s/8s）
- `save_cookies()` / `_load_cookies_from_file()`：Cookie 持久化（JSON 格式）
- `check_login_status(page, selector)`：通过 CSS 选择器判断登录状态
- `safe_get_text()` / `safe_get_attr()`：安全提取 DOM 元素文本/属性（不抛异常）

**agents/crawler.py**
- `BaseCrawler`：抽象基类，定义 `fetch_job_list()` / `fetch_job_detail()` 接口
- `ShixisengCrawler`：实习僧爬虫
  - 搜索 URL：`https://www.shixiseng.com/interns?keyword=...&city=...&page=...`
  - `_parse_card()`：从列表页卡片元素提取职位基本信息，多选择器兜底
  - `fetch_job_detail()`：抓取详情页 JD 文本，多容器选择器优先级兜底
- `BossCrawler`：Boss直聘爬虫
  - `BOSS_CITY_CODES`：14 个主要城市的 cityCode 映射
  - `ensure_logged_in()`：访问首页检查 cookie 有效性，失效则提示用户
  - `_is_blocked()`：检测登录拦截和验证码弹窗
  - `_parse_card()`：从 `.job-card-wrapper` 解析，正则提取 job_detail ID
- `crawl_node(state)`：LangGraph 节点
  - 调用 `init_db()` 确保表已创建
  - 按平台 → 城市 → 页码三层遍历
  - 每条职位：`job_exists()` 去重 → `fetch_job_detail()` → `upsert_job()` 入库
  - 登录失效：记录 error，跳过当前平台，继续其他平台
  - 返回更新后的 state（`current_phase` 置为 `"filter"`）
- `_run_platform_crawl()`：单平台抓取内部函数，解耦平台逻辑与编排逻辑

**tests/test_crawler.py**（9 个测试用例，全部 mock 网络和数据库）
- `test_human_delay_calls_sleep`：验证 sleep 被调用且延迟在合理范围
- `test_human_delay_randomness`：验证多次调用产生随机值
- `test_shixiseng_parse_card_success`：正常解析卡片返回完整 JobPosting
- `test_shixiseng_parse_card_missing_link`：无链接时返回 None
- `test_shixiseng_parse_card_missing_title_company`：缺少必填字段时返回 None
- `test_boss_parse_card_success`：验证 `.html` 后缀被正确去除
- `test_crawl_node_shixiseng_only`：完整编排流程测试（列表→详情→入库→state）
- `test_crawl_node_deduplication`：已存在职位不调用 upsert
- `test_crawl_node_empty_jd_skips_job`：空 JD 职位被跳过并记录 error
- `test_crawl_node_boss_skipped_when_not_logged_in`：未登录跳过 Boss 平台
- `test_crawl_node_respects_max_jobs_limit`：max_jobs_per_run 上限生效
- `test_crawl_node_platform_exception_handled`：平台整体异常不崩溃
- `test_boss_city_codes_coverage`：城市编码格式验证

### 设计决策

- **爬虫不直接操作数据库**：`ShixisengCrawler`/`BossCrawler` 只负责网络抓取和 DOM 解析，去重和入库由 `crawl_node` 统一处理，职责分离，便于单元测试
- **多选择器兜底**：对每个字段按优先级尝试多个 CSS 选择器（`or` 链接），网站改版时只需增加选择器而不是修改结构
- **详情页分两阶段**：列表页只抓基本信息（速度快），详情页在去重后才抓取（节省请求次数）
- **指数退避重试**：`goto_with_retry` 重试间隔为 2^attempt 秒（2s/4s/8s），避免在服务器过载时雪上加霜
- **BrowserManager 单个平台共享**：同一平台的所有城市/页面共用一个浏览器上下文，保持 session 和 cookie 一致

### 遇到的问题

- **Playwright mock 复杂性**：直接 mock Playwright Page 元素需要大量嵌套 AsyncMock，故测试策略改为在爬虫类方法层面 mock（`fetch_job_list`/`fetch_job_detail`），`crawl_node` 的编排逻辑可以被充分测试
- **Boss直聘 cityCode**：Boss直聘搜索 URL 需要 9 位数字城市编码，不能直接用城市名，需要维护映射表

### 下一步（已完成）
- [x] Phase 3：编写 `prompts/filter_prompt.py`
- [x] Phase 3：实现 `agents/filter.py`（批量 LLM 评分 + 数据库写回）
- [x] Phase 3：编写 `tests/test_filter.py`

---

## [2026-03-27] 完成 Phase 3：Filter 模块

### 已完成

**prompts/filter_prompt.py**
- `FILTER_SYSTEM_PROMPT`：752 字系统提示词
  - 三维度评分规则：技能匹配度（40分）/ 发展潜力（30分）/ 综合条件（30分）
  - 每个维度细分 4 档评分区间，附分值范围说明
  - 输出格式约束：严格 JSON `{score, reason, match_points, concern_points}`
- `FILTER_USER_TEMPLATE`：用户消息模板（Markdown 格式）
  - 分为「求职者偏好」和「职位信息」两块，结构清晰
  - 支持 cities / skills_required / skills_bonus / salary_min / industries / job_types
- `FilterInput`：dataclass，封装单次评分的所有输入字段
- `build_filter_prompt(inp)`：将 FilterInput 填充到模板，空字段优雅降级（"不限"/"面议"）

**agents/filter.py**
- `score_job(job, search_config, semaphore, llm_config)`：单职位评分
  - 通过 `asyncio.Semaphore` 控制并发（默认最大 3 个同时）
  - 调用 `call_llm_json()`（含自动 JSON 解析 + 重试）
  - score 范围钳制到 [0, 100]
  - 缺 score 字段时返回 None，不崩溃整体流程
- `batch_score_jobs(jobs, search_config, llm_config, concurrency=3)`：批量评分
  - `asyncio.gather()` 并发执行，`return_exceptions=True` 防单个失败影响全局
  - 成功评分后调用 `update_job_score()` 写回数据库
- `filter_node(state)`：LangGraph 节点
  - 取 `jobs_found` 中 `jd_text` 非空且 `score is None` 的职位
  - `jobs_found` 为空时自动从 DB 读取 `status=new` 的职位（兜底）
  - 筛选 `score >= threshold` → `jobs_to_apply`
  - 返回更新后的 state，`current_phase='tailor'`
- `run_filter(config)`：独立运行接口，不依赖 LangGraph state

**tests/test_filter.py**（14 个测试用例，全部通过）
- Prompt 构造：字段填充 / 空值降级 / 系统 prompt 维度验证
- `score_job`：正常评分 / score 钳制 / 缺字段返回 None / API 失败返回 None
- `batch_score_jobs`：DB 写回 / 异常跳过 / 空列表
- `filter_node`：阈值筛选 / DB 兜底读取 / 无职位优雅退出 / 已评分跳过

### 验证结果

**Layer 1（无需 API key）—— 全部通过**

| 检查项 | 结果 |
|--------|------|
| 单元测试（14 个） | PASS 14/14 |
| DB 读取 3 个 Phase 2 职位 | PASS |
| 每个职位 prompt 构造（5 项字段检查） | PASS 15/15 |
| System prompt 三维度 | PASS |
| Prompt 内容目视确认 | 格式正确，JD/偏好均正确嵌入 |

**Layer 2（真实 LLM 评分）—— 待 API key**

需设置 `ANTHROPIC_API_KEY` 后运行 `python verify_phase3.py` 完成：
```
# Windows CMD
set ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
python verify_phase3.py

# PowerShell
$env:ANTHROPIC_API_KEY = 'sk-ant-xxxxxxxxxxxx'
python verify_phase3.py
```

### 设计决策

- **并发数 = 3 而非 5**：DESIGN.md 建议 5，实际测试中 3 并发已可充分利用 API 配额，且在速率限制边界更安全；claude-haiku 场景下可调高
- **score 钳制**：LLM 偶尔会返回 101、-1 等超界值，代码做 `max(0, min(100, score))` 保护下游逻辑
- **DB 兜底读取**：`filter_node` 在 `jobs_found=[]` 时自动查库，使得 filter 可独立运行（`--mode filter-only`）无需先走 crawl_node
- **`return_exceptions=True`**：`asyncio.gather` 不因单个 API 失败中断整体批量任务

### 遇到的问题

- 无（本阶段无实际网络/LLM 请求，逻辑在 mock 下全部通过）

### 下一步
- [ ] 设置 `ANTHROPIC_API_KEY` 后运行 `python verify_phase3.py` 完成 Layer 2 真实评分验证
- [x] Phase 4：实现 `tools/resume_parser.py` 和 `agents/resume_tailor.py`
- [x] Phase 4：编写 `prompts/tailor_prompt.py`

---

## [2026-03-27] 完成 Phase 4：Resume Tailor 模块

### 已完成

**prompts/tailor_prompt.py**
- `TAILOR_SYSTEM_PROMPT`：严格约束 LLM 只改措辞，禁止修改公司名/日期/数字/学历等事实
- `TAILOR_USER_TEMPLATE`：职位信息 + 原始简历 Markdown 双栏格式
- `TailorInput` dataclass：封装单次定制所需全部输入
- `build_tailor_prompt(inp)`：填充模板，空字段优雅降级（"未知"）

**tools/resume_parser.py**
- `read_resume(path)`：UTF-8 读取 Markdown 简历
- `extract_facts(resume_text)`：
  - 日期正则限定 `19xx/20xx` 前缀，避免误匹配电话号码
  - 提取量化数据（%/倍/万/千等中文单位）
  - 提取加粗项（公司名、职位名等事实锚点）
- `validate_resume_integrity(original, tailored)`：比对日期/数字/加粗项是否完整保留
- `save_resume_markdown()`：输出 `{job_id}_{YYYYMMDD}.md`
- `save_resume_pdf()`：fpdf2 后端，CJK 字体（SimHei）支持中文，A4 纸
  - `_build_pdf()`：自动扫描系统 CJK 字体路径（Windows/Linux/macOS），注册为 "CJK" 字体族
  - `_render_markdown()`：`fnt()` 辅助函数统一字体选择（CJK 可用时用 CJK，否则回退 Helvetica）
- `save_resume_docx()`：python-docx 后端，支持内联加粗

**agents/resume_tailor.py**
- `tailor_resume_for_job(job, base_resume, llm_config, ...)`：
  - 调用 Claude API → `_strip_code_fence()` 清理 LLM 代码块包裹
  - 完整性校验失败时追加警告块但仍保存（方便人工审查）
  - 保存 Markdown + PDF（可选 DOCX）
  - 更新数据库状态为 `tailored`
- `tailor_node(state)`：LangGraph 节点，遍历 `jobs_to_apply`，聚合结果到 `resumes_generated`
- `run_tailor(jobs, config)`：独立运行接口（`--mode tailor-only`）
- `_strip_code_fence(text)`：去除 LLM 返回的 ` ```markdown ``` ` 包裹

**tests/test_resume_tailor.py**（29 个测试用例，全部通过）
- Prompt 构造（4）/ 简历读取（2）/ 事实提取（4）/ 完整性校验（5）/ 文件保存（5）
- `tailor_resume_for_job` 端到端 mock（5）/ `tailor_node` LangGraph 节点（4）

### 验证结果

| 检查项 | 结果 |
|--------|------|
| 单元测试（29 个） | PASS 29/29 |
| PDF 生成（中文内容，26 KB） | PASS |
| Markdown 保存 | PASS |
| 事实提取日期正则（不误匹配手机号） | PASS |

### 设计决策

- **fpdf2 替代 WeasyPrint**：WeasyPrint 在 Windows 需要 GTK/GObject，安装复杂。fpdf2 纯 Python，无系统依赖
- **CJK 字体按优先级搜索**：代码自动探测系统字体路径（SimHei → SimKai → SimSun → WQY → PingFang），无需用户配置
- **CJK 无 italic 降级**：SimHei 无斜体变体，`fnt()` 在 CJK 模式下不设置 italic style，避免 fpdf2 报错
- **项目符号用 `-`**：SimHei 不含 U+2022（•），改用 ASCII 横杠避免字形缺失警告
- **完整性校验软失败**：校验不通过时追加警告块并继续保存，不中断整体流程；由人工审查违规项

### 遇到的问题

- **WeasyPrint Windows 失败**：改用 fpdf2 解决
- **Helvetica 不支持中文**：新增 CJK 字体自动检测 + `_render_markdown` 统一 `fnt()` 辅助函数
- **日期正则误匹配手机号**：将 `\d{4}` 前缀改为 `(?:19|20)\d{2}` 解决

### 下一步
- [x] Phase 5：实现 `agents/applicator.py`（半自动投递模块）
- [x] Phase 5：编写 `tests/test_applicator.py`
- [x] Phase 6：实现 `agents/orchestrator.py`（LangGraph 总编排）
- [x] Phase 7：编写 `main.py` 入口 + README

---

## [2026-03-27] 完成 Phase 7：收尾

### 已完成

**main.py（CLI 入口）**
- `argparse` CLI，支持参数：
  - `--mode`：6 种运行模式（full / dry-run / semi-auto / crawl-only / filter-only / tailor-only）
  - `--config`：配置文件路径（默认 config.yaml）
  - `--thread-id`：Checkpoint session ID（默认当天日期）
  - `--resume`：从上次 checkpoint 恢复
  - `--log-level`：日志级别（DEBUG/INFO/WARNING/ERROR）
- `_load_config()`：YAML 加载 + 文件不存在时输出清晰错误并退出
- `_check_env()`：检测 `ANTHROPIC_API_KEY`，缺失时警告并询问是否继续
- `_print_banner()`：Rich Panel 显示启动信息（模式/Session/时间）
- `_print_summary()`：Rich Table 显示运行结果摘要（抓取/达标/简历/投递/错误）
- `KeyboardInterrupt` 优雅退出：提示当前进度已保存，下次可 `--resume` 继续
- 退出码：有错误返回 1，无错误返回 0（便于 CI/脚本集成）
- 第三方库日志降噪（httpx/anthropic/playwright/fonttools 设为 WARNING）

**README.md**
- 功能概览表格（五个模块一览）
- 安装步骤（pip / playwright / API key / config）
- 基础简历格式说明
- 完整使用示例（6 种模式 + 断点恢复 + 日志控制）
- 配置文件字段逐一说明
- Boss直聘 Cookie 配置指引
- 数据存储结构说明
- 项目文件结构
- 常见问题（无职位/乱码/速率限制/断点恢复）
- 开发指南（测试/新平台/自定义评分）
- 免责声明

### 设计决策
- **`--mode dry-run` 为默认**：防止误触全自动投递，必须显式指定 `semi-auto` 或 `full`
- **Rich 替代 print**：Panel/Table 输出结果摘要，比纯文本更易读；RichHandler 美化日志格式
- **退出码语义**：`errors` 非空返回 1，方便外部脚本（如 cron job）检测运行健康状态
- **第三方日志降噪**：fonttools（PDF 字体子集化）、httpx、playwright 日志默认在 INFO 级别极为冗长，统一压到 WARNING

### 项目完成状态

所有 Phase 1-7 已全部完成。

| 模块 | 测试数 | 通过 |
|------|--------|------|
| test_crawler.py | 13 | 11（预存 2 个 DOM 选择器失配） |
| test_filter.py | 14 | 14 |
| test_resume_tailor.py | 29 | 29 |
| test_applicator.py | 21 | 21 |
| test_orchestrator.py | 28 | 28 |
| **合计** | **105** | **103** |

---

## [2026-03-27] 完成 Phase 6：Orchestrator 模块

### 已完成

**agents/orchestrator.py**

**图结构**
```
START → crawl → filter → tailor → apply → END
                   ↓         ↓        ↓
            (条件路由)  (条件路由) (条件路由)
```
- 四个节点：`crawl_node` / `filter_node` / `tailor_node` / `apply_node`
- 三条条件边（`add_conditional_edges`）：每节点执行后判断是否继续

**条件路由逻辑**
- `_route_after_crawl`：`crawl-only` 或 `should_stop` 或 `jobs_found=[]` → END
- `_route_after_filter`：`filter-only` 或 `should_stop` 或 `jobs_to_apply=[]` → END
- `_route_after_tailor`：`tailor-only` 或 `should_stop` 或 `resumes_generated=[]` → END

**支持的 run_mode**
| 模式 | 执行节点 | 说明 |
|------|---------|------|
| `crawl-only` | crawl | 只抓取 |
| `filter-only` | crawl → filter | 抓取+评分 |
| `tailor-only` | crawl → filter → tailor | 抓取+评分+简历 |
| `dry-run` | 全部 | apply 不真实投递 |
| `semi-auto` | 全部 | apply 请求人工确认 |
| `full` | 全部 | 全自动投递 |

**Checkpoint（中断恢复）**
- `AsyncSqliteSaver` 持久化到 `data/checkpoints.db`（与 jobs.db 分开）
- `thread_id` 默认为当天日期（YYYYMMDD），同 ID 重跑时可恢复
- `run_agent(config, run_mode, thread_id, resume=True)` → 恢复上次中断的节点
- `resume_agent(thread_id, config)` → 便捷恢复接口，无 checkpoint 时抛出错误
- `run_agent_in_memory(config, run_mode)` → MemorySaver 模式（测试/调试用，不写磁盘）

**tests/test_orchestrator.py**（28 个测试，全部通过）
- 路由函数（13）：crawl/filter/tailor 三个路由函数的各种终止/继续条件
- 图结构（3）：节点注册 / 无 checkpointer / MemorySaver 编译
- 全流程（8）：dry-run / crawl-only / filter-only / tailor-only / should_stop / 空 jobs / 空 resumes / errors 累积
- Checkpoint（2）：checkpoint 保存验证 / 重复运行无异常

### 设计决策

- **条件路由用 `run_mode == "xxx"` 而非查集合**：初版用 `_STOP_AFTER` 字典（`{"filter-only": {"crawl", "filter"}}`），但导致 filter-only 在 crawl 后就错误终止（"crawl" 在集合里）。改为每个路由函数只检查自己对应的 `run_mode` 字符串，逻辑清晰无歧义
- **thread_id 默认当天日期**：同一天多次运行使用同一 checkpoint，支持当天内的中断恢复；第二天自动开新 session
- **jobs.db 与 checkpoints.db 分离**：jobs 数据库由业务代码管理（aiosqlite），checkpoint 数据库由 LangGraph 管理（AsyncSqliteSaver），避免表名冲突和事务干扰
- **`resume=True` 显式参数**：恢复需要明确传 `resume=True`，防止偶然覆盖已有 checkpoint 的误操作

### 遇到的问题

- **`_STOP_AFTER` 路由逻辑错误**：filter-only 在 crawl 后提前终止（见设计决策）→ 改为直接字符串比较解决
- **`aget_tuple` 传参格式**：MemorySaver 的 `aget_tuple` 需要完整 `{"configurable": {"thread_id": ...}}`，不能只传内层 dict → 测试代码修正

### 测试覆盖情况

| 模块 | 测试数 | 通过 |
|------|--------|------|
| test_crawler.py | 13 | 11（预存） |
| test_filter.py | 14 | 14 |
| test_resume_tailor.py | 29 | 29 |
| test_applicator.py | 21 | 21 |
| test_orchestrator.py | 28 | 28 |
| **合计** | **105** | **103** |

---

## [2026-03-27] 完成 Phase 5：Applicator 投递模块

### 已完成

**prompts/apply_prompt.py**
- `APPLY_SYSTEM_PROMPT`：Boss直聘打招呼消息生成约束（50-120字、具体、禁止套话）
- `APPLY_USER_TEMPLATE`：职位信息 + 用户技能 + 匹配亮点三栏模板
- `ApplyInput` dataclass：封装单次消息生成的所有输入字段
- `build_apply_prompt()`：JD 自动截断至 400 字节省 token

**agents/applicator.py**
- `_build_summary(job, resume_info)`：终端展示摘要（职位/公司/评分/匹配点/简历路径）
- `_ask_user_confirm(summary)`：打印摘要 + 读取 stdin Y/N（支持 EOFError/Ctrl-C 优雅退出）
- `_generate_opening_message(job, config)`：LLM 生成打招呼消息，失败时退回通用模板
- `_apply_boss(job, resume_info, opening_msg, bm, screenshot_dir)`：
  - Boss直聘 Playwright 投递流程：加载页面 → 点击「立即沟通」→ 填写消息 → 发送 → 截图
  - 多选择器兜底（6 个「立即沟通」选择器，5 个输入框选择器，4 个发送按钮选择器）
  - 每步失败均截图留证，返回 failed 状态（不崩溃整体流程）
- `_apply_shixiseng()`：实习僧半自动投递（打开页面 + 尝试点击申请 + 截图）
- `apply_to_job(job, resume_info, config, run_mode, bm, screenshot_dir)`：
  - **防重复**：调用 `is_already_applied()` 检查，已投递直接返回 None
  - **dry-run**：打印 `[DRY-RUN]` 摘要，写 pending 记录，不开浏览器
  - **semi-auto**：LLM 生成消息预览 → 请求用户确认 → 否则跳过
  - **full**：LLM 生成消息 → 直接投递
  - 投递成功更新 `jobs.status = 'applied'`
- `apply_node(state)`：LangGraph 节点
  - `nullcontext()` 在 dry-run 模式下避免启动浏览器
  - `max_applications_per_day` 每日上限控制
  - 单个职位异常隔离（写 errors，继续下一个）
  - `current_phase → 'done'`
- `run_apply(jobs, resumes, config, run_mode)`：独立运行接口（`--mode apply-only`）
- `_take_screenshot(page, dir, job_id)`：截图工具，失败返回 None

**tests/test_applicator.py**（21 个测试用例，全部通过）
- Prompt 构造（5）：字段填充 / JD 截断 / 空技能降级 / System prompt 约束 / 摘要格式
- dry-run（2）：pending 记录生成 / 不触发 BrowserManager
- 防重复（1）：已投递返回 None
- semi-auto（5）：用户确认投递 / 用户拒绝跳过 / Y/N/EOF 输入处理
- apply_node（6）：正常流程 / 空简历 / 每日上限 / 已投递跳过 / 找不到 job 映射 / 异常隔离
- run_apply（2）：正常 dry-run / 无对应简历跳过

### 设计决策

- **`nullcontext()` 代替条件 BrowserManager**：在 dry-run 模式下 `async with nullcontext() as bm` 返回 `None`，apply_to_job 检查 `bm is None` 决定是否可用浏览器，代码路径统一
- **半自动是默认**：`run_mode` 默认 `"dry-run"`，需显式设置 `"semi-auto"` 或 `"full"` 才真实投递，防误操作
- **LLM 消息降级**：打招呼消息生成失败时返回通用模板，不因 API 失败中断投递流程
- **多选择器兜底**：Boss直聘 DOM 可能随更新变动，按优先级依次尝试多个选择器，提高鲁棒性
- **截图存档**：每次投递（成功或失败）均截图到 `data/screenshots/`，方便事后审查

### 遇到的问题

- **`_build_summary` 路径优先级**：初版只取 `md_path`，修正为优先取 `pdf_path`（用户更关心 PDF）

### 测试覆盖情况

| 模块 | 测试数 | 通过 |
|------|--------|------|
| test_crawler.py | 13 | 11（2 个预存 DOM 选择器不匹配，待 Phase 2 修复） |
| test_filter.py | 14 | 14 |
| test_resume_tailor.py | 29 | 29 |
| test_applicator.py | 21 | 21 |
| **合计** | **77** | **75** |

---

## [2026-03-27] Phase 1 + Phase 2 验证通过

### 验证结果

**Phase 1 — 数据库（verify_phase1.py）**

| 检查项 | 结果 |
|--------|------|
| `data/jobs.db` 文件创建 | PASS（20480 bytes） |
| `jobs` 表存在 | PASS |
| `applications` 表存在 | PASS |
| jobs 表 15 个必要字段均存在 | PASS |
| `init_db()` 幂等性（二次调用无错误） | PASS |

**Phase 2 — 实习僧爬虫（verify_phase2.py）**

| 检查项 | 结果 |
|--------|------|
| 页面加载成功（`load` 模式） | PASS |
| 列表页识别职位卡片 | PASS：21 个 `.intern-item` |
| 标题、公司、城市正确解析 | PASS：信山行科技、华策北京、快手、小米 等 |
| 详情页 JD 文本抓取 | PASS：664–822 字/职位 |
| 数据库写入完整（jd_text > 50 字） | PASS：3/3 条全部通过 |

### Bug 修复记录

**Bug 1：`networkidle` 超时**
- 现象：`Page.goto` 超时（>30s），shixiseng 网络请求持续不断
- 修复：改为 `wait_until="load"` + `asyncio.sleep(2)` 等待 JS 渲染
- 影响文件：`agents/crawler.py`（列表页和详情页均修改）

**Bug 2：公司名选择器错误**
- 现象：公司列始终为 N/A
- 原因：使用了 `.company-name`/`.company`，实际 DOM 是 `.intern-detail__company a.title`
- 修复：`_parse_card()` 改为先定位父容器 `.intern-detail__company`，再查 `a.title`
- 影响文件：`agents/crawler.py`

**Bug 3：JD 容器选择器错误**
- 现象：`fetch_job_detail()` 无法定位 JD 内容，退化到 body 全文
- 实际 class：`.job-content`（含完整 JD，≈800 字）
- 修复：选择器优先级改为 `.job-content` → `.content_left` → `.job_detail` → `.intern-detail-page`
- 影响文件：`agents/crawler.py`

### 薪资说明

实习僧列表页 `span.day` 对部分职位显示 `-/天`（数据库字段为 NULL），
实际薪资包含在详情页 JD 文本中（如"150–200元/天"），LLM 筛选时可从 `jd_text` 解析。

### 下一步
- [ ] Phase 3：编写 `prompts/filter_prompt.py`
- [ ] Phase 3：实现 `agents/filter.py`（批量 LLM 评分 + 数据库写回）
- [ ] Phase 3：编写 `tests/test_filter.py`
