"""
tests/test_orchestrator.py — Orchestrator 模块单元测试

策略：
  - 路由函数：纯函数直接测试，无 mock
  - 图结构：检查节点和边是否注册正确
  - 全流程：mock 四个节点函数，使用 MemorySaver，验证状态流转
  - Checkpoint：验证 run_agent_in_memory 在不同 run_mode 下的终止节点
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.memory import MemorySaver

from agents.orchestrator import (
    _route_after_crawl,
    _route_after_filter,
    _route_after_tailor,
    build_graph,
    run_agent_in_memory,
)
from langgraph.graph import END
from models.agent_state import AgentState, make_initial_state
from models.job_posting import JobPosting


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


def _make_job(job_id: str = "boss_001") -> JobPosting:
    return JobPosting(
        id=job_id,
        title="算法工程师",
        company="字节跳动",
        location="北京",
        jd_text="要求 Python、LLM",
        platform="boss",
        url="https://example.com/job/001",
        crawled_at=datetime.now(),
        score=85.0,
        status="tailored",
    )


def _make_resume(job_id: str = "boss_001") -> dict:
    return {
        "job_id": job_id,
        "job_title": "算法工程师",
        "job_company": "字节跳动",
        "md_path": f"data/outputs/{job_id}.md",
        "pdf_path": f"data/outputs/{job_id}.pdf",
        "integrity_ok": True,
        "violations": [],
    }


def _base_config(run_mode: str = "dry-run") -> dict:
    return {
        "user": {"name": "张三", "base_resume_path": "data/base_resume.md"},
        "search": {
            "cities": ["北京"],
            "skills_required": ["Python"],
            "filter_score_threshold": 65,
        },
        "platforms": {"boss": {"enabled": True, "cookie_file": ".cookies/boss.json"}},
        "limits": {"max_jobs_per_run": 10, "max_applications_per_day": 5},
        "llm": {"model": "claude-sonnet-4-20250514"},
    }


def _state_with(**kwargs) -> AgentState:
    """创建含指定字段的 AgentState（其余字段取默认值）。"""
    state = make_initial_state(_base_config(), run_mode=kwargs.get("run_mode", "dry-run"))
    for k, v in kwargs.items():
        state[k] = v
    return state


# ---------------------------------------------------------------------------
# 1. 路由函数测试（纯逻辑，无异步）
# ---------------------------------------------------------------------------


class TestRouteAfterCrawl:
    def test_should_stop_returns_end(self):
        state = _state_with(should_stop=True, jobs_found=[_make_job()])
        assert _route_after_crawl(state) == END

    def test_crawl_only_returns_end(self):
        state = _state_with(run_mode="crawl-only", jobs_found=[_make_job()])
        assert _route_after_crawl(state) == END

    def test_empty_jobs_returns_end(self):
        state = _state_with(jobs_found=[])
        assert _route_after_crawl(state) == END

    def test_normal_proceeds_to_filter(self):
        state = _state_with(run_mode="dry-run", jobs_found=[_make_job()])
        assert _route_after_crawl(state) == "filter"

    def test_semi_auto_proceeds_to_filter(self):
        state = _state_with(run_mode="semi-auto", jobs_found=[_make_job()])
        assert _route_after_crawl(state) == "filter"


class TestRouteAfterFilter:
    def test_should_stop_returns_end(self):
        state = _state_with(should_stop=True, jobs_to_apply=[_make_job()])
        assert _route_after_filter(state) == END

    def test_filter_only_returns_end(self):
        state = _state_with(run_mode="filter-only", jobs_to_apply=[_make_job()])
        assert _route_after_filter(state) == END

    def test_empty_jobs_to_apply_returns_end(self):
        state = _state_with(run_mode="dry-run", jobs_to_apply=[])
        assert _route_after_filter(state) == END

    def test_normal_proceeds_to_tailor(self):
        state = _state_with(run_mode="dry-run", jobs_to_apply=[_make_job()])
        assert _route_after_filter(state) == "tailor"

    def test_crawl_only_already_stopped_before_filter(self):
        """crawl-only 在 crawl 后就终止，不会到达 filter 路由，但即使到达也应终止。"""
        # crawl-only 不在 filter 路由的判断里，走正常路径
        state = _state_with(run_mode="crawl-only", jobs_to_apply=[_make_job()])
        # crawl-only 不匹配 filter-only，所以此处走 tailor
        assert _route_after_filter(state) == "tailor"


class TestRouteAfterTailor:
    def test_should_stop_returns_end(self):
        state = _state_with(should_stop=True, resumes_generated=[_make_resume()])
        assert _route_after_tailor(state) == END

    def test_tailor_only_returns_end(self):
        state = _state_with(run_mode="tailor-only", resumes_generated=[_make_resume()])
        assert _route_after_tailor(state) == END

    def test_empty_resumes_returns_end(self):
        state = _state_with(run_mode="dry-run", resumes_generated=[])
        assert _route_after_tailor(state) == END

    def test_normal_proceeds_to_apply(self):
        state = _state_with(run_mode="dry-run", resumes_generated=[_make_resume()])
        assert _route_after_tailor(state) == "apply"

    def test_full_mode_proceeds_to_apply(self):
        state = _state_with(run_mode="full", resumes_generated=[_make_resume()])
        assert _route_after_tailor(state) == "apply"


# ---------------------------------------------------------------------------
# 2. 图结构测试
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_graph_has_all_nodes(self):
        graph = build_graph()
        nodes = set(graph.nodes.keys())
        assert "crawl" in nodes
        assert "filter" in nodes
        assert "tailor" in nodes
        assert "apply" in nodes

    def test_graph_compiles_without_checkpointer(self):
        graph = build_graph(checkpointer=None)
        assert graph is not None

    def test_graph_compiles_with_memory_saver(self):
        saver = MemorySaver()
        graph = build_graph(checkpointer=saver)
        assert graph is not None


# ---------------------------------------------------------------------------
# 3. 全流程测试（mock 四个节点，MemorySaver）
# ---------------------------------------------------------------------------


class TestRunAgentInMemory:
    """通过 mock 节点函数，验证状态在整个图中的正确流转。"""

    def _make_crawl_result(self, state: AgentState) -> AgentState:
        """模拟 crawl_node：填充 jobs_found。"""
        return {
            **state,
            "jobs_found": [_make_job()],
            "current_phase": "filter",
        }

    def _make_filter_result(self, state: AgentState) -> AgentState:
        """模拟 filter_node：填充 jobs_to_apply。"""
        return {
            **state,
            "jobs_filtered": [_make_job()],
            "jobs_to_apply": [_make_job()],
            "current_phase": "tailor",
        }

    def _make_tailor_result(self, state: AgentState) -> AgentState:
        """模拟 tailor_node：填充 resumes_generated。"""
        return {
            **state,
            "resumes_generated": [_make_resume()],
            "current_phase": "apply",
        }

    def _make_apply_result(self, state: AgentState) -> AgentState:
        """模拟 apply_node：填充 applications_sent。"""
        return {
            **state,
            "applications_sent": [{"job_id": "boss_001", "status": "pending"}],
            "current_phase": "done",
        }

    @pytest.mark.asyncio
    async def test_full_workflow_dry_run(self):
        """dry-run 模式：全部 4 个节点执行，最终 current_phase=done。"""
        config = _base_config("dry-run")

        with (
            patch("agents.orchestrator.crawl_node", side_effect=self._make_crawl_result),
            patch("agents.orchestrator.filter_node", side_effect=self._make_filter_result),
            patch("agents.orchestrator.tailor_node", side_effect=self._make_tailor_result),
            patch("agents.orchestrator.apply_node", side_effect=self._make_apply_result),
        ):
            final = await run_agent_in_memory(config, run_mode="dry-run")

        assert final["current_phase"] == "done"
        assert len(final["jobs_found"]) == 1
        assert len(final["jobs_to_apply"]) == 1
        assert len(final["resumes_generated"]) == 1
        assert len(final["applications_sent"]) == 1

    @pytest.mark.asyncio
    async def test_crawl_only_stops_after_crawl(self):
        """crawl-only 模式：执行 crawl 后终止，不执行 filter/tailor/apply。"""
        config = _base_config("crawl-only")

        filter_mock = AsyncMock()
        tailor_mock = AsyncMock()
        apply_mock = AsyncMock()

        with (
            patch("agents.orchestrator.crawl_node", side_effect=self._make_crawl_result),
            patch("agents.orchestrator.filter_node", filter_mock),
            patch("agents.orchestrator.tailor_node", tailor_mock),
            patch("agents.orchestrator.apply_node", apply_mock),
        ):
            final = await run_agent_in_memory(config, run_mode="crawl-only")

        filter_mock.assert_not_called()
        tailor_mock.assert_not_called()
        apply_mock.assert_not_called()
        assert len(final["jobs_found"]) == 1

    @pytest.mark.asyncio
    async def test_filter_only_stops_after_filter(self):
        """filter-only 模式：执行 crawl+filter 后终止。"""
        config = _base_config("filter-only")

        tailor_mock = AsyncMock()
        apply_mock = AsyncMock()

        with (
            patch("agents.orchestrator.crawl_node", side_effect=self._make_crawl_result),
            patch("agents.orchestrator.filter_node", side_effect=self._make_filter_result),
            patch("agents.orchestrator.tailor_node", tailor_mock),
            patch("agents.orchestrator.apply_node", apply_mock),
        ):
            final = await run_agent_in_memory(config, run_mode="filter-only")

        tailor_mock.assert_not_called()
        apply_mock.assert_not_called()
        assert len(final["jobs_to_apply"]) == 1

    @pytest.mark.asyncio
    async def test_tailor_only_stops_after_tailor(self):
        """tailor-only 模式：执行 crawl+filter+tailor 后终止。"""
        config = _base_config("tailor-only")

        apply_mock = AsyncMock()

        with (
            patch("agents.orchestrator.crawl_node", side_effect=self._make_crawl_result),
            patch("agents.orchestrator.filter_node", side_effect=self._make_filter_result),
            patch("agents.orchestrator.tailor_node", side_effect=self._make_tailor_result),
            patch("agents.orchestrator.apply_node", apply_mock),
        ):
            final = await run_agent_in_memory(config, run_mode="tailor-only")

        apply_mock.assert_not_called()
        assert len(final["resumes_generated"]) == 1

    @pytest.mark.asyncio
    async def test_should_stop_after_crawl(self):
        """crawl 返回 should_stop=True 时，流程立即终止。"""
        config = _base_config("dry-run")

        def crawl_stop(state):
            return {**state, "should_stop": True, "jobs_found": [], "current_phase": "filter"}

        filter_mock = AsyncMock()
        with (
            patch("agents.orchestrator.crawl_node", side_effect=crawl_stop),
            patch("agents.orchestrator.filter_node", filter_mock),
        ):
            final = await run_agent_in_memory(config)

        filter_mock.assert_not_called()
        assert final["should_stop"] is True

    @pytest.mark.asyncio
    async def test_no_jobs_to_apply_skips_tailor_and_apply(self):
        """filter 后 jobs_to_apply 为空 → tailor 和 apply 不执行。"""
        config = _base_config("dry-run")

        def filter_empty(state):
            return {
                **state,
                "jobs_filtered": [_make_job()],
                "jobs_to_apply": [],   # 无达标职位
                "current_phase": "tailor",
            }

        tailor_mock = AsyncMock()
        apply_mock = AsyncMock()
        with (
            patch("agents.orchestrator.crawl_node", side_effect=self._make_crawl_result),
            patch("agents.orchestrator.filter_node", side_effect=filter_empty),
            patch("agents.orchestrator.tailor_node", tailor_mock),
            patch("agents.orchestrator.apply_node", apply_mock),
        ):
            final = await run_agent_in_memory(config)

        tailor_mock.assert_not_called()
        apply_mock.assert_not_called()
        assert final["jobs_to_apply"] == []

    @pytest.mark.asyncio
    async def test_no_resumes_skips_apply(self):
        """tailor 后 resumes_generated 为空 → apply 不执行。"""
        config = _base_config("dry-run")

        def tailor_empty(state):
            return {**state, "resumes_generated": [], "current_phase": "apply"}

        apply_mock = AsyncMock()
        with (
            patch("agents.orchestrator.crawl_node", side_effect=self._make_crawl_result),
            patch("agents.orchestrator.filter_node", side_effect=self._make_filter_result),
            patch("agents.orchestrator.tailor_node", side_effect=tailor_empty),
            patch("agents.orchestrator.apply_node", apply_mock),
        ):
            final = await run_agent_in_memory(config)

        apply_mock.assert_not_called()
        assert final["resumes_generated"] == []

    @pytest.mark.asyncio
    async def test_errors_accumulate_across_nodes(self):
        """各节点产生的 errors 在最终 state 中完整保留。"""
        config = _base_config("dry-run")

        def crawl_with_error(state):
            return {
                **state,
                "jobs_found": [_make_job()],
                "errors": [{"module": "crawl", "error": "超时", "job_id": None}],
                "current_phase": "filter",
            }

        def filter_with_error(state):
            return {
                **state,
                "jobs_to_apply": [_make_job()],
                "errors": state.get("errors", []) + [{"module": "filter", "error": "LLM失败", "job_id": "boss_001"}],
                "current_phase": "tailor",
            }

        with (
            patch("agents.orchestrator.crawl_node", side_effect=crawl_with_error),
            patch("agents.orchestrator.filter_node", side_effect=filter_with_error),
            patch("agents.orchestrator.tailor_node", side_effect=self._make_tailor_result),
            patch("agents.orchestrator.apply_node", side_effect=self._make_apply_result),
        ):
            final = await run_agent_in_memory(config)

        assert len(final["errors"]) == 2
        modules = {e["module"] for e in final["errors"]}
        assert modules == {"crawl", "filter"}


# ---------------------------------------------------------------------------
# 4. Checkpoint 持久化测试（MemorySaver）
# ---------------------------------------------------------------------------


class TestCheckpoint:
    @pytest.mark.asyncio
    async def test_graph_saves_checkpoint_after_each_node(self):
        """验证 MemorySaver 在节点执行后保存了 checkpoint。"""
        config = _base_config("crawl-only")
        saver = MemorySaver()
        compiled = build_graph(checkpointer=saver)
        thread_cfg = {"configurable": {"thread_id": "test-ckpt-001"}}
        initial = make_initial_state(config, run_mode="crawl-only")

        def crawl_simple(state):
            return {**state, "jobs_found": [_make_job()], "current_phase": "filter"}

        with patch("agents.orchestrator.crawl_node", side_effect=crawl_simple):
            await compiled.ainvoke(initial, config=thread_cfg)

        # 验证 checkpoint 已保存（aget_tuple 需要完整的 RunnableConfig）
        ckpt = await saver.aget_tuple(thread_cfg)
        assert ckpt is not None

    @pytest.mark.asyncio
    async def test_graph_can_be_rerun_with_memory_saver(self):
        """MemorySaver 模式下连续两次运行不报错（不要求状态共享，只验证无异常）。"""
        config = _base_config("crawl-only")
        saver = MemorySaver()

        def crawl_simple(state):
            return {**state, "jobs_found": [_make_job()], "current_phase": "filter"}

        with patch("agents.orchestrator.crawl_node", side_effect=crawl_simple):
            r1 = await run_agent_in_memory(config, run_mode="crawl-only")
            r2 = await run_agent_in_memory(config, run_mode="crawl-only")

        assert len(r1["jobs_found"]) == 1
        assert len(r2["jobs_found"]) == 1
