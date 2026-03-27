"""
Filter 模块单元测试。

测试策略：
- Mock LLM 调用（避免真实 API 费用和网络依赖）
- Mock 数据库操作
- 重点测试：评分逻辑、并发控制、JSON 校验、DB 写回、阈值筛选
"""

from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

import pytest

from models.agent_state import make_initial_state
from models.job_posting import JobPosting
from prompts.filter_prompt import FilterInput, build_filter_prompt, FILTER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_job(job_id="shixiseng_test001", title="Python AI 实习生",
             company="测试科技", score=None, jd_text=""):
    return JobPosting(
        id=job_id,
        title=title,
        company=company,
        location="北京",
        salary_range="200元/天",
        jd_text=jd_text or f"岗位职责：{title}相关工作。要求熟悉Python、LLM开发经验优先。",
        platform="shixiseng",
        url=f"https://www.shixiseng.com/intern/{job_id}",
        crawled_at=datetime(2026, 3, 27),
        score=score,
    )


SAMPLE_SEARCH_CONFIG = {
    "cities": ["北京"],
    "skills_required": ["Python", "LLM"],
    "skills_bonus": ["LangChain", "RAG"],
    "salary_min": 10000,
    "industries": ["互联网", "AI"],
    "job_types": ["实习"],
    "filter_score_threshold": 65,
}

SAMPLE_LLM_CONFIG = {
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 1024,
    "temperature": 0.2,
}

GOOD_LLM_RESPONSE = {
    "score": 82,
    "reason": "候选人的Python和LLM技能与JD高度匹配，公司为互联网头部企业，薪资和地点均符合预期。",
    "match_points": ["Python匹配", "LLM方向一致", "北京地点匹配"],
    "concern_points": ["实习期较长（6个月）"],
}

LOW_LLM_RESPONSE = {
    "score": 40,
    "reason": "岗位偏向传统制造业，与候选人的AI/LLM技能方向差异较大，薪资也低于预期。",
    "match_points": [],
    "concern_points": ["技能不匹配", "薪资偏低"],
}


# ---------------------------------------------------------------------------
# 测试：build_filter_prompt
# ---------------------------------------------------------------------------

def test_build_filter_prompt_contains_required_fields():
    """构造的 prompt 必须包含职位标题、公司、JD文本、用户技能等关键信息。"""
    inp = FilterInput(
        job_id="test001",
        title="AI 算法实习生",
        company="某某科技",
        location="北京",
        salary_range="300元/天",
        platform="shixiseng",
        jd_text="要求熟悉Python和深度学习框架",
        cities=["北京", "上海"],
        skills_required=["Python", "LLM"],
        skills_bonus=["LangChain"],
        salary_min=10000,
        industries=["AI"],
        job_types=["实习"],
    )
    prompt = build_filter_prompt(inp)

    assert "AI 算法实习生" in prompt
    assert "某某科技" in prompt
    assert "要求熟悉Python和深度学习框架" in prompt
    assert "Python、LLM" in prompt
    assert "北京" in prompt
    assert "300元/天" in prompt


def test_build_filter_prompt_handles_empty_optionals():
    """空的可选字段应优雅降级为默认值，不抛异常。"""
    inp = FilterInput(
        job_id="test002",
        title="实习生",
        company="公司",
        location="",
        salary_range="",
        platform="boss",
        jd_text="JD 文本",
        cities=[],
        skills_required=[],
        skills_bonus=[],
        salary_min=0,
        industries=[],
        job_types=[],
    )
    prompt = build_filter_prompt(inp)
    assert "实习生" in prompt
    assert "不限" in prompt  # 空列表应显示"不限"
    assert "面议" in prompt  # 空薪资应显示"面议"


def test_system_prompt_contains_scoring_dimensions():
    """系统 prompt 应包含三个评分维度的说明。"""
    assert "技能匹配度" in FILTER_SYSTEM_PROMPT
    assert "发展潜力" in FILTER_SYSTEM_PROMPT
    assert "综合条件" in FILTER_SYSTEM_PROMPT
    assert "40" in FILTER_SYSTEM_PROMPT   # 技能匹配度满分
    assert "30" in FILTER_SYSTEM_PROMPT   # 发展潜力满分


# ---------------------------------------------------------------------------
# 测试：score_job
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_score_job_success():
    """正常 LLM 响应时，score_job 应返回含 score/reason 的 dict。"""
    import asyncio
    from agents.filter import score_job

    job = make_job()
    semaphore = asyncio.Semaphore(3)

    with patch("agents.filter.call_llm_json", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = GOOD_LLM_RESPONSE

        result = await score_job(job, SAMPLE_SEARCH_CONFIG, semaphore, SAMPLE_LLM_CONFIG)

    assert result is not None
    assert result["score"] == 82.0
    assert result["job_id"] == job.id
    assert "reason" in result
    assert isinstance(result["match_points"], list)


@pytest.mark.asyncio
async def test_score_job_clamps_score_to_100():
    """LLM 返回超出范围的 score 应被钳制到 [0, 100]。"""
    import asyncio
    from agents.filter import score_job

    job = make_job()
    semaphore = asyncio.Semaphore(1)
    over_response = {**GOOD_LLM_RESPONSE, "score": 150}

    with patch("agents.filter.call_llm_json", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = over_response
        result = await score_job(job, SAMPLE_SEARCH_CONFIG, semaphore, SAMPLE_LLM_CONFIG)

    assert result["score"] == 100.0


@pytest.mark.asyncio
async def test_score_job_returns_none_on_missing_score():
    """LLM 返回 JSON 缺少 score 字段时，应返回 None（不崩溃）。"""
    import asyncio
    from agents.filter import score_job

    job = make_job()
    semaphore = asyncio.Semaphore(1)

    with patch("agents.filter.call_llm_json", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = {"reason": "no score field"}
        result = await score_job(job, SAMPLE_SEARCH_CONFIG, semaphore, SAMPLE_LLM_CONFIG)

    assert result is None


@pytest.mark.asyncio
async def test_score_job_returns_none_on_llm_failure():
    """LLM 调用返回 None（JSON 解析失败）时，score_job 应返回 None。"""
    import asyncio
    from agents.filter import score_job

    job = make_job()
    semaphore = asyncio.Semaphore(1)

    with patch("agents.filter.call_llm_json", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = None
        result = await score_job(job, SAMPLE_SEARCH_CONFIG, semaphore, SAMPLE_LLM_CONFIG)

    assert result is None


# ---------------------------------------------------------------------------
# 测试：batch_score_jobs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_batch_score_jobs_writes_to_db():
    """batch_score_jobs 应对每个成功评分的职位调用 update_job_score。"""
    from agents.filter import batch_score_jobs

    jobs = [make_job(f"shixiseng_j{i}") for i in range(3)]

    with (
        patch("agents.filter.call_llm_json", new_callable=AsyncMock) as mock_llm,
        patch("agents.filter.update_job_score", new_callable=AsyncMock) as mock_update,
    ):
        mock_llm.return_value = GOOD_LLM_RESPONSE

        results = await batch_score_jobs(jobs, SAMPLE_SEARCH_CONFIG, SAMPLE_LLM_CONFIG)

    assert len(results) == 3
    assert mock_update.call_count == 3
    # 验证每次调用的参数
    for call_args in mock_update.call_args_list:
        kwargs = call_args.kwargs
        assert kwargs["score"] == 82.0
        assert kwargs["score_reason"] != ""


@pytest.mark.asyncio
async def test_batch_score_jobs_skips_failed():
    """LLM 调用异常时，batch_score_jobs 应跳过该职位并继续处理其他职位。"""
    from agents.filter import batch_score_jobs

    jobs = [make_job("shixiseng_ok"), make_job("shixiseng_fail")]

    call_count = 0

    async def mock_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return GOOD_LLM_RESPONSE
        raise Exception("模拟 API 调用失败")

    with (
        patch("agents.filter.call_llm_json", side_effect=mock_llm),
        patch("agents.filter.update_job_score", new_callable=AsyncMock),
    ):
        results = await batch_score_jobs(jobs, SAMPLE_SEARCH_CONFIG, SAMPLE_LLM_CONFIG)

    # 只有第一个成功
    assert len(results) == 1
    assert results[0]["job_id"] == "shixiseng_ok"


@pytest.mark.asyncio
async def test_batch_score_jobs_empty_list():
    """空列表输入时，batch_score_jobs 应立即返回空列表。"""
    from agents.filter import batch_score_jobs
    results = await batch_score_jobs([], SAMPLE_SEARCH_CONFIG, SAMPLE_LLM_CONFIG)
    assert results == []


# ---------------------------------------------------------------------------
# 测试：filter_node
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_filter_node_populates_jobs_filtered():
    """filter_node 应根据阈值将达标职位放入 jobs_filtered。"""
    from agents.filter import filter_node

    high_score_job = make_job("shixiseng_high", score=None)
    low_score_job = make_job("shixiseng_low", score=None)

    config = {
        "search": {**SAMPLE_SEARCH_CONFIG, "filter_score_threshold": 65},
        "llm": SAMPLE_LLM_CONFIG,
    }
    state = make_initial_state(config)
    state["jobs_found"] = [high_score_job, low_score_job]

    llm_responses = [GOOD_LLM_RESPONSE, LOW_LLM_RESPONSE]  # 82分, 40分

    call_idx = 0

    async def mock_llm(*args, **kwargs):
        nonlocal call_idx
        resp = llm_responses[call_idx % len(llm_responses)]
        call_idx += 1
        return resp

    # 模拟数据库返回带评分的职位（update_job_score 后 get_top_jobs 的结果）
    scored_jobs = [
        high_score_job.model_copy(update={"score": 82.0, "status": "filtered"}),
        low_score_job.model_copy(update={"score": 40.0, "status": "filtered"}),
    ]

    with (
        patch("agents.filter.call_llm_json", side_effect=mock_llm),
        patch("agents.filter.update_job_score", new_callable=AsyncMock),
        patch("agents.filter.get_jobs_by_status", new_callable=AsyncMock),
        patch("agents.filter.get_top_jobs", new_callable=AsyncMock, return_value=scored_jobs),
    ):
        result_state = await filter_node(state)

    assert result_state["current_phase"] == "tailor"
    assert len(result_state["jobs_filtered"]) == 2  # 所有已评分的
    assert len(result_state["jobs_to_apply"]) == 1   # 只有 82 分那个 >= 65
    assert result_state["jobs_to_apply"][0].score == 82.0


@pytest.mark.asyncio
async def test_filter_node_falls_back_to_db_when_jobs_found_empty():
    """jobs_found 为空时，filter_node 应从数据库读取 status=new 的职位。"""
    from agents.filter import filter_node

    db_job = make_job("shixiseng_db001")
    config = {"search": SAMPLE_SEARCH_CONFIG, "llm": SAMPLE_LLM_CONFIG}
    state = make_initial_state(config)
    state["jobs_found"] = []  # 空

    with (
        patch("agents.filter.call_llm_json", new_callable=AsyncMock, return_value=GOOD_LLM_RESPONSE),
        patch("agents.filter.update_job_score", new_callable=AsyncMock),
        patch("agents.filter.get_jobs_by_status", new_callable=AsyncMock, return_value=[db_job]),
        patch("agents.filter.get_top_jobs", new_callable=AsyncMock,
              return_value=[db_job.model_copy(update={"score": 82.0})]),
    ):
        result_state = await filter_node(state)

    # 数据库中的职位应被处理
    assert len(result_state["jobs_filtered"]) >= 1


@pytest.mark.asyncio
async def test_filter_node_handles_no_jobs_gracefully():
    """无任何待评分职位时，filter_node 应安全返回空列表，不报错。"""
    from agents.filter import filter_node

    config = {"search": SAMPLE_SEARCH_CONFIG, "llm": SAMPLE_LLM_CONFIG}
    state = make_initial_state(config)
    state["jobs_found"] = []

    with (
        patch("agents.filter.get_jobs_by_status", new_callable=AsyncMock, return_value=[]),
        patch("agents.filter.get_top_jobs", new_callable=AsyncMock, return_value=[]),
    ):
        result_state = await filter_node(state)

    assert result_state["jobs_filtered"] == []
    assert result_state["jobs_to_apply"] == []
    assert result_state["current_phase"] == "tailor"


@pytest.mark.asyncio
async def test_filter_node_skips_already_scored_jobs():
    """jobs_found 中已有 score 的职位不应重复评分。"""
    from agents.filter import filter_node

    already_scored = make_job("shixiseng_scored", score=75.0)
    unscored = make_job("shixiseng_new", score=None)

    config = {"search": SAMPLE_SEARCH_CONFIG, "llm": SAMPLE_LLM_CONFIG}
    state = make_initial_state(config)
    state["jobs_found"] = [already_scored, unscored]

    llm_call_count = 0

    async def mock_llm(*args, **kwargs):
        nonlocal llm_call_count
        llm_call_count += 1
        return GOOD_LLM_RESPONSE

    with (
        patch("agents.filter.call_llm_json", side_effect=mock_llm),
        patch("agents.filter.update_job_score", new_callable=AsyncMock),
        patch("agents.filter.get_jobs_by_status", new_callable=AsyncMock),
        patch("agents.filter.get_top_jobs", new_callable=AsyncMock,
              return_value=[already_scored, unscored.model_copy(update={"score": 82.0})]),
    ):
        await filter_node(state)

    # 只有 unscored 需要调用 LLM（already_scored 跳过）
    assert llm_call_count == 1
