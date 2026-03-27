# Job Hunt Agent

##项目制作：冯子恒 1879118503@qq.com

> 求职自动化 AI Agent：抓取岗位 → 智能筛选 → 定制简历 → 自动投递

用 LangGraph 编排四个 AI 节点，全自动完成从搜索职位到发送投递的完整求职流程。支持实习僧和 Boss直聘两大平台。

---

## 功能概览

| 模块 | 功能 |
|------|------|
| **爬虫（Crawler）** | Playwright 抓取实习僧/Boss直聘职位列表和 JD，反检测处理，Cookie 持久化 |
| **筛选（Filter）** | Claude 对每个 JD 从技能/成长/条件三维度打分（0-100），并发批量处理 |
| **简历定制（Tailor）** | Claude 针对每个职位重写简历措辞，输出 Markdown + PDF，完整性校验防止 LLM 篡改事实 |
| **投递（Applicator）** | 半自动（人工确认）或全自动投递，截图存档，防重复检查，每日投递限额 |
| **编排（Orchestrator）** | LangGraph StateGraph 串联全流程，SQLite checkpoint 支持断点恢复 |

---

## 环境要求

- Python 3.11+
- [Anthropic API Key](https://console.anthropic.com/)（筛选/简历/投递阶段需要）
- Windows / macOS / Linux（PDF 生成需要系统中文字体：Windows 自带 SimHei，Linux 安装 `fonts-wqy-microhei`）

---

## 安装

**1. 克隆仓库并安装依赖**

```bash
git clone <repo-url>
cd job-hunt-agent
pip install -e ".[dev]"
```

**2. 安装 Playwright 浏览器**

```bash
playwright install chromium
```

**3. 设置 API Key**

```bash
# Windows CMD
set ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx

# PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-xxxxxxxxxxxx"

# Linux / macOS
export ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx
```

**4. 初始化配置文件**

复制示例配置并按实际情况修改：

```bash
cp config.yaml config.yaml.bak  # 备份（已有内容可跳过）
```

编辑 `config.yaml`，至少修改以下字段：

```yaml
user:
  name: "你的姓名"
  email: "your@email.com"
  phone: "138xxxx1234"
  base_resume_path: "data/base_resume.md"   # 你的基础简历

search:
  cities: ["北京", "上海"]                  # 目标城市
  skills_required: ["Python", "LLM"]        # 必须技能
  filter_score_threshold: 65               # 低于此分数跳过
```

**5. 准备基础简历**

将你的简历以 Markdown 格式保存到 `data/base_resume.md`。Agent 会基于此生成针对每个职位的定制版本。

---

## 快速开始

### 全流程演练（推荐初次使用）

```bash
python main.py --mode dry-run
```

`dry-run` 是默认模式：运行完整流程但不真实投递，只在数据库中写入 `pending` 记录。用于验证配置和简历质量。

### 查看帮助

```bash
python main.py --help
```

---

## 使用说明

### 运行模式

| 命令 | 说明 |
|------|------|
| `python main.py` | dry-run（全流程演练，默认） |
| `python main.py --mode semi-auto` | 半自动：每个职位投递前终端确认 |
| `python main.py --mode full` | 全自动投递（⚠️ 谨慎使用） |
| `python main.py --mode crawl-only` | 只抓取职位（不需要 API Key） |
| `python main.py --mode filter-only` | 抓取 + LLM 评分 |
| `python main.py --mode tailor-only` | 抓取 + 评分 + 生成简历 |

### 断点恢复

每个节点执行完成后，状态自动保存到 `data/checkpoints.db`。如果程序中途被中断：

```bash
# 下次运行时加 --resume，从上次完成的节点继续
python main.py --resume

# 恢复指定日期的 session（thread-id 默认为当天日期 YYYYMMDD）
python main.py --thread-id 20260327 --resume
```

### 日志级别

```bash
python main.py --log-level DEBUG    # 显示所有调试信息（包括 SQL、HTTP）
python main.py --log-level WARNING  # 只显示警告和错误
```

---

## 配置说明

完整配置项见 `config.yaml`，主要字段：

```yaml
user:
  name: "张三"                         # 显示在打招呼消息里
  base_resume_path: "data/base_resume.md"

search:
  cities: ["北京", "上海", "深圳"]
  skills_required: ["Python", "LLM"]   # 必须匹配的技能（影响评分）
  skills_bonus: ["LangChain", "RAG"]   # 加分技能
  filter_score_threshold: 65           # 低于此分数不投递（0-100）
  salary_min: 10000                    # 最低月薪（元）

platforms:
  shixiseng:
    enabled: true                      # 是否启用实习僧
  boss:
    enabled: true                      # 是否启用 Boss直聘
    cookie_file: ".cookies/boss.json"  # Cookie 文件路径

limits:
  max_jobs_per_run: 50                 # 单次最多抓取职位数
  max_applications_per_day: 20         # 每日最多投递数

llm:
  model: "claude-sonnet-4-20250514"   # 使用的 Claude 模型
  temperature: 0.3
```

---

## Boss直聘 Cookie 配置

Boss直聘需要登录 Cookie 才能投递。首次使用：

1. 用浏览器手动登录 Boss直聘
2. 打开开发者工具 → Application → Cookies → 复制所有 cookie
3. 保存到 `.cookies/boss.json`（JSON 格式）

或者将 `headless: false` 改为有头模式让程序自动保存（爬虫首次登录后会自动持久化）。

---

## 数据存储

| 路径 | 说明 |
|------|------|
| `data/jobs.db` | SQLite 主数据库（职位、评分、投递记录） |
| `data/checkpoints.db` | LangGraph checkpoint（断点恢复） |
| `data/outputs/` | 生成的定制简历（Markdown + PDF） |
| `data/screenshots/` | 投递截图存档 |
| `.cookies/` | 平台登录 Cookie（已加入 .gitignore） |

---

## 项目结构

```
job_hunt_agent/
├── main.py                 # CLI 入口
├── config.yaml             # 用户配置
├── agents/
│   ├── crawler.py          # 职位抓取（实习僧 + Boss直聘）
│   ├── filter.py           # LLM 智能筛选评分
│   ├── resume_tailor.py    # 简历定制（LLM 重写 + PDF 输出）
│   ├── applicator.py       # 自动投递（半自动 / 全自动）
│   └── orchestrator.py     # LangGraph 总编排 + checkpoint
├── models/
│   ├── job_posting.py      # 职位数据模型（Pydantic）
│   └── agent_state.py      # LangGraph 共享状态
├── prompts/
│   ├── filter_prompt.py    # 筛选评分 prompt
│   ├── tailor_prompt.py    # 简历定制 prompt
│   └── apply_prompt.py     # 打招呼消息 prompt
├── tools/
│   ├── browser.py          # Playwright 封装（反检测、Cookie）
│   ├── llm_client.py       # Claude API 封装（重试、JSON 解析）
│   ├── resume_parser.py    # 简历读写工具（MD/PDF/DOCX）
│   └── db.py               # SQLite 异步操作
├── data/
│   ├── base_resume.md      # 你的基础简历（请替换为真实内容）
│   └── outputs/            # 定制简历输出目录
└── tests/                  # 单元测试（pytest）
```

---

## 安全说明

- **简历事实保护**：`validate_resume_integrity()` 会检查 LLM 是否篡改了公司名、日期、数字等事实信息，校验失败会在文件中添加警告标注
- **防重复投递**：每次投递前检查数据库 `applications` 表，已发送过的职位自动跳过
- **Cookie 安全**：`.cookies/` 目录已加入 `.gitignore`，不会提交到 Git
- **dry-run 默认**：默认模式不真实投递，需显式指定 `--mode semi-auto` 或 `--mode full`

---

## 常见问题

**Q: 运行后没有抓到任何职位？**
- 检查 `config.yaml` 中的 `cities` 和 `search` 关键词是否正确
- 实习僧：无需登录，直接可用
- Boss直聘：检查 `.cookies/boss.json` 是否存在且未过期

**Q: PDF 中中文显示为乱码？**
- Windows：确保系统安装了 SimHei（黑体）或 SimSun（宋体）
- Linux：`sudo apt install fonts-wqy-microhei`

**Q: LLM 调用失败 / 速率限制？**
- 检查 `ANTHROPIC_API_KEY` 是否正确设置
- 降低 `limits.max_jobs_per_run` 减少 API 调用量
- 程序自动重试（最多 3 次，指数退避）

**Q: 中途中断了如何继续？**
```bash
python main.py --resume        # 使用今天的 session
python main.py --thread-id 20260327 --resume  # 使用指定日期的 session
```

**Q: 如何只测试某个模块？**
```bash
python -m pytest tests/test_filter.py -v        # 只测试筛选模块
python -m pytest tests/ -q                      # 运行全部测试
```

---

## 开发指南

### 运行测试

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行全部测试
python -m pytest tests/ -v

# 代码风格检查
ruff check .
```

### 添加新平台

1. 在 `agents/crawler.py` 实现新平台的 `BaseCrawler` 子类
2. 在 `crawl_node()` 中注册新爬虫
3. 在 `config.yaml` 的 `platforms` 下添加配置节
4. 在 `agents/applicator.py` 的 `apply_to_job()` 中添加对应的投递逻辑

### 自定义评分规则

修改 `prompts/filter_prompt.py` 中的 `FILTER_SYSTEM_PROMPT`，调整三个维度（技能/成长/条件）的权重和评分细则。

---

## 免责声明

本工具仅供学习和个人求职使用。使用前请确认：

- 遵守目标平台的用户协议和爬虫条款
- 不要设置过高的并发或过低的延迟，避免对平台造成压力
- 投递内容（简历、打招呼消息）已经人工审核，确保准确无误
- 建议优先使用 `semi-auto` 模式，人工确认每次投递
