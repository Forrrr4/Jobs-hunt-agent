"""
Resume Tailor 模块单元测试。

覆盖：
  - Prompt 构造（tailor_prompt.py）
  - 简历读取与事实提取（resume_parser.py）
  - 完整性校验逻辑（validate_resume_integrity）
  - PDF / DOCX / Markdown 输出（仅验证不崩溃、文件生成）
  - tailor_resume_for_job 端到端（mock LLM）
  - tailor_node LangGraph 节点（mock LLM + DB）
"""

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from models.agent_state import make_initial_state
from models.job_posting import JobPosting
from prompts.tailor_prompt import (
    TAILOR_SYSTEM_PROMPT,
    TailorInput,
    build_tailor_prompt,
)
from tools.resume_parser import (
    extract_facts,
    read_resume,
    save_resume_markdown,
    save_resume_pdf,
    save_resume_docx,
    validate_resume_integrity,
)


# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------

SAMPLE_RESUME = """\
# 张三 — 个人简历

## 基本信息

- **姓名**：张三
- **邮箱**：test@example.com

---

## 教育背景

**北京大学** — 计算机科学与技术，本科
2021.09 — 2025.06（预计）

---

## 技能

- **编程语言**：Python、JavaScript
- **AI/ML**：LLM、RAG、LangChain

---

## 项目经历

### XXX 智能问答系统

2024.01 — 2024.06

- 使用 LangChain + ChromaDB 实现文档检索，准确率提升 30%
- 基于 RAG 架构，处理效率提升 50%

---

## 实习经历

**XX 科技公司** — 后端开发实习生
2024.07 — 2024.09

- 集成 Anthropic API，支持并发请求
"""

SAMPLE_JOB = JobPosting(
    id="shixiseng_test001",
    title="AI 算法实习生",
    company="某某科技",
    location="北京",
    salary_range="200元/天",
    jd_text="要求熟悉 Python、LLM 开发，有 RAG 经验优先，熟悉 LangChain 框架。",
    platform="shixiseng",
    url="https://www.shixiseng.com/intern/test001",
    crawled_at=datetime(2026, 3, 27),
    score=82.0,
)

LLM_CONFIG = {
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 4096,
    "temperature": 0.3,
}


# ---------------------------------------------------------------------------
# 测试：tailor_prompt.py
# ---------------------------------------------------------------------------

class TestTailorPrompt:
    def test_build_prompt_contains_job_info(self):
        """Prompt 必须包含职位名称、公司名称、JD 文本。"""
        inp = TailorInput(
            job_id="test001",
            job_title="AI 算法实习生",
            job_company="某某科技",
            job_location="北京",
            jd_text="要求熟悉 Python 和 LLM",
            resume_text=SAMPLE_RESUME,
        )
        prompt = build_tailor_prompt(inp)
        assert "AI 算法实习生" in prompt
        assert "某某科技" in prompt
        assert "要求熟悉 Python 和 LLM" in prompt
        assert "北京" in prompt

    def test_build_prompt_contains_resume(self):
        """Prompt 必须包含原始简历的关键内容（部分截取验证）。"""
        inp = TailorInput(
            job_id="test001",
            job_title="AI 实习",
            job_company="公司",
            job_location="上海",
            jd_text="JD 内容",
            resume_text=SAMPLE_RESUME,
        )
        prompt = build_tailor_prompt(inp)
        assert "张三" in prompt
        assert "北京大学" in prompt
        assert "LangChain" in prompt

    def test_system_prompt_contains_forbidden_rules(self):
        """系统 prompt 必须明确列出禁止修改的内容类别。"""
        assert "严格禁止" in TAILOR_SYSTEM_PROMPT
        assert "公司名称" in TAILOR_SYSTEM_PROMPT
        assert "量化数据" in TAILOR_SYSTEM_PROMPT
        assert "学历" in TAILOR_SYSTEM_PROMPT

    def test_system_prompt_contains_allowed_rules(self):
        """系统 prompt 必须说明允许修改的内容。"""
        assert "允许" in TAILOR_SYSTEM_PROMPT
        assert "关键词" in TAILOR_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 测试：resume_parser.py — 读取
# ---------------------------------------------------------------------------

class TestReadResume:
    def test_read_existing_file(self, tmp_path):
        """读取存在的文件应返回正确内容。"""
        f = tmp_path / "resume.md"
        f.write_text("# 我的简历\n\n内容", encoding="utf-8")
        content = read_resume(f)
        assert "我的简历" in content

    def test_read_nonexistent_file_raises(self, tmp_path):
        """读取不存在的文件应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            read_resume(tmp_path / "nonexistent.md")


# ---------------------------------------------------------------------------
# 测试：extract_facts
# ---------------------------------------------------------------------------

class TestExtractFacts:
    def test_extracts_dates(self):
        """应提取时间段和年份。"""
        facts = extract_facts(SAMPLE_RESUME)
        assert any("2021" in d for d in facts["dates"])
        assert any("2024" in d for d in facts["dates"])

    def test_extracts_numbers(self):
        """应提取百分比等量化数据。"""
        facts = extract_facts(SAMPLE_RESUME)
        assert "30%" in facts["numbers"]
        assert "50%" in facts["numbers"]

    def test_extracts_bold_items(self):
        """应提取加粗项（公司名、学校名等）。"""
        facts = extract_facts(SAMPLE_RESUME)
        assert "北京大学" in facts["bold_items"]
        assert "XX 科技公司" in facts["bold_items"]

    def test_empty_resume_returns_empty_facts(self):
        facts = extract_facts("")
        assert facts["dates"] == []
        assert facts["numbers"] == []
        assert facts["bold_items"] == []


# ---------------------------------------------------------------------------
# 测试：validate_resume_integrity
# ---------------------------------------------------------------------------

class TestValidateIntegrity:
    def test_identical_resume_passes(self):
        """完全相同的简历应通过校验。"""
        is_valid, violations = validate_resume_integrity(SAMPLE_RESUME, SAMPLE_RESUME)
        assert is_valid
        assert violations == []

    def test_detects_missing_date(self):
        """删除日期应被检测为违规。"""
        tampered = SAMPLE_RESUME.replace("2021.09", "REMOVED")
        is_valid, violations = validate_resume_integrity(SAMPLE_RESUME, tampered)
        assert not is_valid
        assert any("2021" in v for v in violations)

    def test_detects_missing_number(self):
        """删除量化数字应被检测为违规。"""
        tampered = SAMPLE_RESUME.replace("30%", "显著")
        is_valid, violations = validate_resume_integrity(SAMPLE_RESUME, tampered)
        assert not is_valid
        assert any("30%" in v for v in violations)

    def test_detects_missing_company(self):
        """删除公司名（加粗项）应被检测为违规。"""
        tampered = SAMPLE_RESUME.replace("**北京大学**", "某大学")
        is_valid, violations = validate_resume_integrity(SAMPLE_RESUME, tampered)
        assert not is_valid
        assert any("北京大学" in v for v in violations)

    def test_rephrased_descriptions_pass(self):
        """仅修改描述措辞（保留所有事实）应通过校验。"""
        # 修改项目描述措辞，但保留日期/数字/加粗项
        tailored = SAMPLE_RESUME.replace(
            "实现文档检索，准确率提升 30%",
            "构建高效文档检索系统，检索准确率提升 30%（关键词优化）",
        )
        is_valid, violations = validate_resume_integrity(SAMPLE_RESUME, tailored)
        assert is_valid, f"误报违规：{violations}"


# ---------------------------------------------------------------------------
# 测试：文件输出
# ---------------------------------------------------------------------------

class TestSaveResume:
    def test_save_markdown(self, tmp_path):
        """Markdown 应被正确保存，内容不变。"""
        path = save_resume_markdown(SAMPLE_RESUME, tmp_path, "test_job_001")
        assert path.exists()
        assert path.suffix == ".md"
        content = path.read_text(encoding="utf-8")
        assert "张三" in content

    def test_save_markdown_filename_format(self, tmp_path):
        """文件名应符合 {job_id}_{date}.md 格式。"""
        path = save_resume_markdown(SAMPLE_RESUME, tmp_path, "shixiseng_abc123", "20260327")
        assert "shixiseng_abc123" in path.name
        assert "20260327" in path.name

    def test_save_pdf_creates_file(self, tmp_path):
        """PDF 文件应被创建（仅验证不崩溃和文件存在）。"""
        path = save_resume_pdf(SAMPLE_RESUME, tmp_path, "test_job_001", "20260327")
        if path:  # fpdf2 可能在某些环境下不可用
            assert path.exists()
            assert path.suffix == ".pdf"
            assert path.stat().st_size > 0

    def test_save_docx_creates_file(self, tmp_path):
        """DOCX 文件应被创建且可被 python-docx 读取。"""
        path = save_resume_docx(SAMPLE_RESUME, tmp_path, "test_job_001", "20260327")
        if path:
            assert path.exists()
            assert path.suffix == ".docx"
            from docx import Document
            doc = Document(str(path))
            full_text = "\n".join(p.text for p in doc.paragraphs)
            assert "张三" in full_text

    def test_save_creates_output_dir(self, tmp_path):
        """输出目录不存在时应自动创建。"""
        nested = tmp_path / "a" / "b" / "c"
        path = save_resume_markdown(SAMPLE_RESUME, nested, "job001")
        assert path.exists()


# ---------------------------------------------------------------------------
# 测试：tailor_resume_for_job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tailor_resume_for_job_success(tmp_path):
    """正常 LLM 响应时应生成简历文件并返回结果 dict。"""
    from agents.resume_tailor import tailor_resume_for_job

    mock_tailored = SAMPLE_RESUME.replace(
        "使用 LangChain + ChromaDB 实现文档检索",
        "基于 LangChain + ChromaDB 构建 RAG 文档检索系统",
    )

    with (
        patch("agents.resume_tailor.call_llm", new_callable=AsyncMock,
              return_value=mock_tailored),
        patch("agents.resume_tailor.update_job_status", new_callable=AsyncMock),
    ):
        result = await tailor_resume_for_job(
            job=SAMPLE_JOB,
            base_resume=SAMPLE_RESUME,
            llm_config=LLM_CONFIG,
            output_dir=tmp_path,
        )

    assert result is not None
    assert result["job_id"] == SAMPLE_JOB.id
    assert result["integrity_ok"] is True
    assert result["md_path"] is not None
    assert Path(result["md_path"]).exists()


@pytest.mark.asyncio
async def test_tailor_resume_strips_code_fence(tmp_path):
    """LLM 返回被 ```markdown ... ``` 包裹时，应自动剥离代码块标记。"""
    from agents.resume_tailor import tailor_resume_for_job

    wrapped = f"```markdown\n{SAMPLE_RESUME}\n```"

    with (
        patch("agents.resume_tailor.call_llm", new_callable=AsyncMock, return_value=wrapped),
        patch("agents.resume_tailor.update_job_status", new_callable=AsyncMock),
    ):
        result = await tailor_resume_for_job(
            job=SAMPLE_JOB,
            base_resume=SAMPLE_RESUME,
            llm_config=LLM_CONFIG,
            output_dir=tmp_path,
        )

    assert result is not None
    # 文件中不应包含 ```
    md_content = Path(result["md_path"]).read_text(encoding="utf-8")
    assert "```" not in md_content


@pytest.mark.asyncio
async def test_tailor_resume_detects_tampering(tmp_path):
    """LLM 修改了量化数字时，完整性校验应失败并在文件中写入警告。"""
    from agents.resume_tailor import tailor_resume_for_job

    tampered = SAMPLE_RESUME.replace("准确率提升 30%", "准确率大幅提升")

    with (
        patch("agents.resume_tailor.call_llm", new_callable=AsyncMock, return_value=tampered),
        patch("agents.resume_tailor.update_job_status", new_callable=AsyncMock),
    ):
        result = await tailor_resume_for_job(
            job=SAMPLE_JOB,
            base_resume=SAMPLE_RESUME,
            llm_config=LLM_CONFIG,
            output_dir=tmp_path,
        )

    assert result is not None
    assert result["integrity_ok"] is False
    assert len(result["violations"]) > 0
    # 警告应写入文件
    md_content = Path(result["md_path"]).read_text(encoding="utf-8")
    assert "完整性警告" in md_content


@pytest.mark.asyncio
async def test_tailor_resume_returns_none_on_llm_error(tmp_path):
    """LLM 调用抛出异常时，应返回 None 且不崩溃。"""
    from agents.resume_tailor import tailor_resume_for_job

    with patch("agents.resume_tailor.call_llm", new_callable=AsyncMock,
               side_effect=Exception("模拟 API 超时")):
        result = await tailor_resume_for_job(
            job=SAMPLE_JOB,
            base_resume=SAMPLE_RESUME,
            llm_config=LLM_CONFIG,
            output_dir=tmp_path,
        )

    assert result is None


@pytest.mark.asyncio
async def test_tailor_resume_returns_none_on_empty_response(tmp_path):
    """LLM 返回空字符串时应返回 None。"""
    from agents.resume_tailor import tailor_resume_for_job

    with (
        patch("agents.resume_tailor.call_llm", new_callable=AsyncMock, return_value=""),
        patch("agents.resume_tailor.update_job_status", new_callable=AsyncMock),
    ):
        result = await tailor_resume_for_job(
            job=SAMPLE_JOB,
            base_resume=SAMPLE_RESUME,
            llm_config=LLM_CONFIG,
            output_dir=tmp_path,
        )

    assert result is None


# ---------------------------------------------------------------------------
# 测试：tailor_node（LangGraph 节点）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tailor_node_success(tmp_path):
    """正常流程：两个职位都成功定制，state 正确更新。"""
    from agents.resume_tailor import tailor_node

    job1 = SAMPLE_JOB
    job2 = SAMPLE_JOB.model_copy(update={"id": "shixiseng_test002", "title": "后端实习"})

    config = {
        "user": {"base_resume_path": str(tmp_path / "resume.md")},
        "llm": LLM_CONFIG,
    }
    # 写入临时简历文件
    (tmp_path / "resume.md").write_text(SAMPLE_RESUME, encoding="utf-8")

    state = make_initial_state(config)
    state["jobs_to_apply"] = [job1, job2]

    with (
        patch("agents.resume_tailor.call_llm", new_callable=AsyncMock,
              return_value=SAMPLE_RESUME),
        patch("agents.resume_tailor.update_job_status", new_callable=AsyncMock),
        patch("agents.resume_tailor.OUTPUT_DIR", tmp_path),
    ):
        result_state = await tailor_node(state)

    assert result_state["current_phase"] == "apply"
    assert len(result_state["resumes_generated"]) == 2
    assert all("md_path" in r for r in result_state["resumes_generated"])


@pytest.mark.asyncio
async def test_tailor_node_missing_resume_sets_should_stop(tmp_path):
    """基础简历文件不存在时，should_stop 应置为 True。"""
    from agents.resume_tailor import tailor_node

    config = {
        "user": {"base_resume_path": str(tmp_path / "nonexistent.md")},
        "llm": LLM_CONFIG,
    }
    state = make_initial_state(config)
    state["jobs_to_apply"] = [SAMPLE_JOB]

    result_state = await tailor_node(state)

    assert result_state["should_stop"] is True
    assert len(result_state["errors"]) >= 1


@pytest.mark.asyncio
async def test_tailor_node_empty_jobs_to_apply(tmp_path):
    """jobs_to_apply 为空时，应安全返回空 resumes_generated。"""
    from agents.resume_tailor import tailor_node

    (tmp_path / "resume.md").write_text(SAMPLE_RESUME, encoding="utf-8")
    config = {
        "user": {"base_resume_path": str(tmp_path / "resume.md")},
        "llm": LLM_CONFIG,
    }
    state = make_initial_state(config)
    state["jobs_to_apply"] = []

    result_state = await tailor_node(state)

    assert result_state["resumes_generated"] == []
    assert result_state["current_phase"] == "apply"


@pytest.mark.asyncio
async def test_tailor_node_one_fails_others_continue(tmp_path):
    """单个职位 LLM 失败时，其余职位应继续处理（不中断）。"""
    from agents.resume_tailor import tailor_node

    (tmp_path / "resume.md").write_text(SAMPLE_RESUME, encoding="utf-8")
    config = {
        "user": {"base_resume_path": str(tmp_path / "resume.md")},
        "llm": LLM_CONFIG,
    }

    job1 = SAMPLE_JOB
    job2 = SAMPLE_JOB.model_copy(update={"id": "shixiseng_test002", "title": "后端实习"})

    state = make_initial_state(config)
    state["jobs_to_apply"] = [job1, job2]

    call_count = 0

    async def mock_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("第一个职位 LLM 失败")
        return SAMPLE_RESUME

    with (
        patch("agents.resume_tailor.call_llm", side_effect=mock_llm),
        patch("agents.resume_tailor.update_job_status", new_callable=AsyncMock),
        patch("agents.resume_tailor.OUTPUT_DIR", tmp_path),
    ):
        result_state = await tailor_node(state)

    # job1 失败，job2 成功
    assert len(result_state["resumes_generated"]) == 1
    assert result_state["resumes_generated"][0]["job_id"] == job2.id
    assert len(result_state["errors"]) >= 1
