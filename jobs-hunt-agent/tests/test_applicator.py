"""
tests/test_applicator.py — Applicator 模块单元测试

策略：
  - 全程 mock 网络（BrowserManager）、数据库（tools.db.*）、LLM（call_llm）
  - 覆盖：Prompt 构造 / dry-run / semi-auto 确认&跳过 / 防重复 / 限额 / 节点编排
"""

import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from models.agent_state import make_initial_state
from models.job_posting import JobPosting
from prompts.apply_prompt import (
    APPLY_SYSTEM_PROMPT,
    ApplyInput,
    build_apply_prompt,
)
from agents.applicator import (
    _build_summary,
    _ask_user_confirm,
    apply_to_job,
    apply_node,
    run_apply,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_job(
    job_id: str = "boss_001",
    platform: str = "boss",
    score: float = 82.0,
    match_points: list[str] | None = None,
) -> JobPosting:
    return JobPosting(
        id=job_id,
        title="后端开发实习生",
        company="字节跳动",
        location="北京",
        salary_range="200元/天",
        jd_text="要求 Python、Go、LangChain，有 LLM 项目经验优先。",
        platform=platform,
        url="https://www.zhipin.com/job_detail/001",
        crawled_at=datetime.now(),
        score=score,
        score_reason="技能高度匹配",
        match_points=match_points or ["Python", "LLM"],
        concern_points=["需要实习6个月"],
        status="tailored",
    )


def _make_resume_info(job_id: str = "boss_001") -> dict:
    return {
        "job_id": job_id,
        "job_title": "后端开发实习生",
        "job_company": "字节跳动",
        "md_path": f"data/outputs/{job_id}_20260327.md",
        "pdf_path": f"data/outputs/{job_id}_20260327.pdf",
        "integrity_ok": True,
        "violations": [],
    }


def _make_config(run_mode: str = "dry-run") -> dict:
    return {
        "user": {"name": "张三", "email": "zhangsan@example.com"},
        "search": {
            "skills_required": ["Python", "LLM"],
            "skills_bonus": ["LangChain"],
        },
        "platforms": {
            "boss": {"cookie_file": ".cookies/boss.json", "enabled": True},
        },
        "limits": {"max_applications_per_day": 20},
        "llm": {"model": "claude-sonnet-4-20250514", "max_tokens": 2048, "temperature": 0.3},
    }


# ---------------------------------------------------------------------------
# 1. Prompt 构造测试
# ---------------------------------------------------------------------------


class TestApplyPrompt:
    def test_build_prompt_contains_job_info(self):
        inp = ApplyInput(
            job_title="算法工程师",
            job_company="快手",
            job_location="上海",
            jd_text="熟悉 Python、PyTorch，有推荐系统经验。" * 20,
            user_name="张三",
            skills=["Python", "PyTorch"],
            match_points=["推荐系统经验"],
        )
        prompt = build_apply_prompt(inp)
        assert "算法工程师" in prompt
        assert "快手" in prompt
        assert "上海" in prompt
        assert "Python" in prompt
        assert "推荐系统经验" in prompt

    def test_jd_truncated_to_400_chars(self):
        long_jd = "A" * 800
        inp = ApplyInput(
            job_title="工程师",
            job_company="公司",
            job_location="北京",
            jd_text=long_jd,
            user_name="张三",
        )
        prompt = build_apply_prompt(inp)
        # 截断后应带省略号
        assert "..." in prompt
        # prompt 中不应出现 800 个连续的 A
        assert "A" * 600 not in prompt

    def test_empty_skills_fallback(self):
        inp = ApplyInput(
            job_title="工程师",
            job_company="公司",
            job_location="北京",
            jd_text="JD 内容",
            user_name="张三",
            skills=[],
            match_points=[],
        )
        prompt = build_apply_prompt(inp)
        assert "见简历" in prompt
        assert "技能与 JD 高度匹配" in prompt

    def test_system_prompt_constraints(self):
        # System prompt 应包含长度要求和禁止项
        assert "50" in APPLY_SYSTEM_PROMPT
        assert "120" in APPLY_SYSTEM_PROMPT
        assert "禁止" in APPLY_SYSTEM_PROMPT

    def test_build_summary_format(self):
        job = _make_job()
        resume_info = _make_resume_info()
        summary = _build_summary(job, resume_info)
        assert "后端开发实习生" in summary
        assert "字节跳动" in summary
        assert "82" in summary   # 分数
        assert "Python" in summary  # 匹配点
        assert "boss_001_20260327.pdf" in summary


# ---------------------------------------------------------------------------
# 2. dry-run 模式
# ---------------------------------------------------------------------------


class TestApplyDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_creates_pending_record(self):
        job = _make_job()
        resume_info = _make_resume_info()
        config = _make_config()

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
            patch("agents.applicator.insert_application", new_callable=AsyncMock, return_value=1),
        ):
            result = await apply_to_job(job, resume_info, config, run_mode="dry-run")

        assert result is not None
        assert result["status"] == "pending"
        assert result["job_id"] == "boss_001"
        assert result["notes"] == "dry-run 模式"
        assert result["application_db_id"] == 1

    @pytest.mark.asyncio
    async def test_dry_run_no_browser_needed(self):
        """dry-run 不应调用 BrowserManager。"""
        job = _make_job()
        resume_info = _make_resume_info()
        config = _make_config()

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
            patch("agents.applicator.insert_application", new_callable=AsyncMock, return_value=2),
            patch("agents.applicator.BrowserManager") as mock_bm,
        ):
            result = await apply_to_job(job, resume_info, config, run_mode="dry-run", bm=None)

        mock_bm.assert_not_called()  # 不应创建 BrowserManager
        assert result["status"] == "pending"


# ---------------------------------------------------------------------------
# 3. 防重复投递
# ---------------------------------------------------------------------------


class TestAlreadyApplied:
    @pytest.mark.asyncio
    async def test_skip_when_already_applied(self):
        job = _make_job()
        resume_info = _make_resume_info()
        config = _make_config()

        with patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=True):
            result = await apply_to_job(job, resume_info, config, run_mode="dry-run")

        assert result is None  # 返回 None 表示跳过


# ---------------------------------------------------------------------------
# 4. semi-auto 模式（用户确认 / 跳过）
# ---------------------------------------------------------------------------


class TestSemiAuto:
    @pytest.mark.asyncio
    async def test_semi_auto_user_confirms(self):
        """用户输入 y → 触发投递，返回 sent 记录。"""
        job = _make_job()
        resume_info = _make_resume_info()
        config = _make_config()

        mock_bm = AsyncMock()
        mock_page = AsyncMock()
        mock_bm.new_page = AsyncMock(return_value=mock_page)
        mock_bm.goto_with_retry = AsyncMock(return_value=True)

        # 模拟「立即沟通」按钮可见
        mock_btn = AsyncMock()
        mock_btn.is_visible = AsyncMock(return_value=True)
        mock_page.query_selector = AsyncMock(return_value=mock_btn)

        # 模拟聊天输入框
        mock_input = AsyncMock()
        mock_page.wait_for_selector = AsyncMock(return_value=mock_input)
        mock_page.screenshot = AsyncMock()

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
            patch("agents.applicator.insert_application", new_callable=AsyncMock, return_value=3),
            patch("agents.applicator.update_job_status", new_callable=AsyncMock),
            patch("agents.applicator.call_llm", new_callable=AsyncMock, return_value="您好，我对该职位很感兴趣…"),
            patch("agents.applicator._ask_user_confirm", return_value=True),
        ):
            result = await apply_to_job(job, resume_info, config, run_mode="semi-auto", bm=mock_bm)

        assert result is not None
        assert result["status"] == "sent"

    @pytest.mark.asyncio
    async def test_semi_auto_user_declines(self):
        """用户输入 n → 跳过，返回 None。"""
        job = _make_job()
        resume_info = _make_resume_info()
        config = _make_config()

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
            patch("agents.applicator.call_llm", new_callable=AsyncMock, return_value="打招呼消息"),
            patch("agents.applicator._ask_user_confirm", return_value=False),
        ):
            result = await apply_to_job(job, resume_info, config, run_mode="semi-auto", bm=MagicMock())

        assert result is None

    def test_ask_user_confirm_yes(self):
        with patch("builtins.input", return_value="y"):
            assert _ask_user_confirm("摘要内容") is True

    def test_ask_user_confirm_no(self):
        with patch("builtins.input", return_value=""):
            assert _ask_user_confirm("摘要内容") is False

    def test_ask_user_confirm_eof(self):
        with patch("builtins.input", side_effect=EOFError):
            assert _ask_user_confirm("摘要内容") is False


# ---------------------------------------------------------------------------
# 5. apply_node LangGraph 节点
# ---------------------------------------------------------------------------


class TestApplyNode:
    def _make_state(self, run_mode: str = "dry-run", resumes: list[dict] | None = None) -> dict:
        state = make_initial_state(_make_config(), run_mode=run_mode)
        job = _make_job()
        state["jobs_to_apply"] = [job]
        state["resumes_generated"] = resumes if resumes is not None else [_make_resume_info()]
        return state

    @pytest.mark.asyncio
    async def test_apply_node_dry_run(self):
        """dry-run 模式：所有职位均生成 pending 记录，current_phase=done。"""
        state = self._make_state(run_mode="dry-run")

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
            patch("agents.applicator.insert_application", new_callable=AsyncMock, return_value=1),
        ):
            new_state = await apply_node(state)

        assert new_state["current_phase"] == "done"
        assert len(new_state["applications_sent"]) == 1
        assert new_state["applications_sent"][0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_apply_node_empty_resumes(self):
        """无简历时直接返回，不报错。"""
        state = self._make_state(resumes=[])
        new_state = await apply_node(state)
        assert new_state["current_phase"] == "done"
        assert new_state["applications_sent"] == []

    @pytest.mark.asyncio
    async def test_apply_node_respects_max_limit(self):
        """每日投递上限生效：超过 max_per_day 的职位被跳过。"""
        state = self._make_state(run_mode="dry-run")
        # 造 5 条简历，但上限设为 2
        state["config"]["limits"]["max_applications_per_day"] = 2
        jobs = [_make_job(job_id=f"boss_{i:03d}") for i in range(5)]
        resumes = [_make_resume_info(job_id=f"boss_{i:03d}") for i in range(5)]
        state["jobs_to_apply"] = jobs
        state["resumes_generated"] = resumes

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
            patch("agents.applicator.insert_application", new_callable=AsyncMock, return_value=1),
        ):
            new_state = await apply_node(state)

        assert len(new_state["applications_sent"]) == 2

    @pytest.mark.asyncio
    async def test_apply_node_skips_already_applied(self):
        """已投递过的职位全部跳过，applications_sent 为空。"""
        state = self._make_state(run_mode="dry-run")

        with patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=True):
            new_state = await apply_node(state)

        assert new_state["applications_sent"] == []
        assert new_state["current_phase"] == "done"

    @pytest.mark.asyncio
    async def test_apply_node_missing_job_in_map(self):
        """简历中 job_id 找不到对应 JobPosting 时，跳过但不崩溃。"""
        state = self._make_state(run_mode="dry-run")
        # 故意不放 jobs_to_apply
        state["jobs_to_apply"] = []

        new_state = await apply_node(state)
        assert new_state["current_phase"] == "done"
        assert len(new_state["applications_sent"]) == 0

    @pytest.mark.asyncio
    async def test_apply_node_exception_isolated(self):
        """单个职位异常不影响其他职位，错误被记录到 errors。"""
        state = self._make_state(run_mode="dry-run")

        # 让 is_already_applied 抛出异常
        with patch(
            "agents.applicator.is_already_applied",
            new_callable=AsyncMock,
            side_effect=RuntimeError("DB 连接失败"),
        ):
            new_state = await apply_node(state)

        assert new_state["current_phase"] == "done"
        assert len(new_state["errors"]) == 1
        assert "DB 连接失败" in new_state["errors"][0]["error"]


# ---------------------------------------------------------------------------
# 6. run_apply 独立接口
# ---------------------------------------------------------------------------


class TestRunApply:
    @pytest.mark.asyncio
    async def test_run_apply_dry_run(self):
        jobs = [_make_job()]
        resumes = [_make_resume_info()]
        config = _make_config()

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
            patch("agents.applicator.insert_application", new_callable=AsyncMock, return_value=1),
        ):
            results = await run_apply(jobs, resumes, config, run_mode="dry-run")

        assert len(results) == 1
        assert results[0]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_run_apply_skips_job_without_resume(self):
        """无对应简历的职位被跳过。"""
        jobs = [_make_job(job_id="boss_999")]
        resumes = [_make_resume_info(job_id="boss_000")]  # 不匹配
        config = _make_config()

        with (
            patch("agents.applicator.is_already_applied", new_callable=AsyncMock, return_value=False),
        ):
            results = await run_apply(jobs, resumes, config, run_mode="dry-run")

        assert results == []
