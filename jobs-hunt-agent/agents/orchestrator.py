"""
LangGraph 总编排模块。

流程图：
    START → crawl → filter → tailor → apply → END

条件路由（每个节点执行后判断）：
  - should_stop=True        → 立即终止（遇到严重错误）
  - run_mode=crawl-only     → crawl 后终止
  - run_mode=filter-only    → filter 后终止
  - run_mode=tailor-only    → tailor 后终止
  - jobs_to_apply 为空      → filter 后终止（无达标职位，无需继续）
  - resumes_generated 为空  → tailor 后终止（无简历生成，跳过投递）

Checkpoint（中断恢复）：
  - 使用 AsyncSqliteSaver 将每个节点完成后的 AgentState 持久化到
    data/checkpoints.db（与 jobs.db 分开存储）
  - 同 thread_id 重新调用 run_agent() 时，从上次完成的节点之后恢复执行
  - thread_id 默认为当天日期（YYYYMMDD），可通过参数覆盖以支持多次运行
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from agents.applicator import apply_node
from agents.crawler import crawl_node
from agents.filter import filter_node
from agents.resume_tailor import tailor_node
from models.agent_state import AgentState, make_initial_state

logger = logging.getLogger(__name__)

CHECKPOINT_DB = Path("data/checkpoints.db")


# ---------------------------------------------------------------------------
# 条件路由函数
# ---------------------------------------------------------------------------


def _route_after_crawl(state: AgentState) -> Literal["filter", "__end__"]:
    """
    crawl_node 完成后的路由决策。

    终止条件：
      - should_stop=True（爬虫严重错误）
      - run_mode=crawl-only
      - jobs_found 为空（无任何职位抓取到）
    """
    if state.get("should_stop"):
        logger.info("[Orchestrator] should_stop=True，终止于 crawl 阶段")
        return END

    run_mode = state.get("run_mode", "dry-run")
    if run_mode == "crawl-only":
        logger.info("[Orchestrator] run_mode=crawl-only，crawl 后终止")
        return END

    if not state.get("jobs_found"):
        logger.warning("[Orchestrator] jobs_found 为空，提前终止")
        return END

    return "filter"


def _route_after_filter(state: AgentState) -> Literal["tailor", "__end__"]:
    """
    filter_node 完成后的路由决策。

    终止条件：
      - should_stop=True
      - run_mode=filter-only
      - jobs_to_apply 为空（无职位达到评分阈值）
    """
    if state.get("should_stop"):
        logger.info("[Orchestrator] should_stop=True，终止于 filter 阶段")
        return END

    run_mode = state.get("run_mode", "dry-run")
    if run_mode == "filter-only":
        logger.info("[Orchestrator] run_mode=filter-only，filter 后终止")
        return END

    if not state.get("jobs_to_apply"):
        logger.warning("[Orchestrator] jobs_to_apply 为空（无达标职位），提前终止")
        return END

    return "tailor"


def _route_after_tailor(state: AgentState) -> Literal["apply", "__end__"]:
    """
    tailor_node 完成后的路由决策。

    终止条件：
      - should_stop=True
      - run_mode=tailor-only
      - resumes_generated 为空（无简历成功生成）
    """
    if state.get("should_stop"):
        logger.info("[Orchestrator] should_stop=True，终止于 tailor 阶段")
        return END

    run_mode = state.get("run_mode", "dry-run")
    if run_mode == "tailor-only":
        logger.info("[Orchestrator] run_mode=tailor-only，tailor 后终止")
        return END

    if not state.get("resumes_generated"):
        logger.warning("[Orchestrator] resumes_generated 为空，跳过 apply 阶段")
        return END

    return "apply"


# ---------------------------------------------------------------------------
# 图构建
# ---------------------------------------------------------------------------


def build_graph(checkpointer=None):
    """
    构建并编译 LangGraph StateGraph。

    Args:
        checkpointer: 可选 checkpoint 后端（MemorySaver / AsyncSqliteSaver）。
                      传 None 则不持久化（测试用）。

    Returns:
        CompiledStateGraph（可直接调用 ainvoke / astream）
    """
    graph = StateGraph(AgentState)

    # 注册节点
    graph.add_node("crawl", crawl_node)
    graph.add_node("filter", filter_node)
    graph.add_node("tailor", tailor_node)
    graph.add_node("apply", apply_node)

    # 入口边
    graph.add_edge(START, "crawl")

    # 条件边
    graph.add_conditional_edges(
        "crawl",
        _route_after_crawl,
        {"filter": "filter", END: END},
    )
    graph.add_conditional_edges(
        "filter",
        _route_after_filter,
        {"tailor": "tailor", END: END},
    )
    graph.add_conditional_edges(
        "tailor",
        _route_after_tailor,
        {"apply": "apply", END: END},
    )

    # apply 执行完即结束
    graph.add_edge("apply", END)

    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# 主入口：带 checkpoint 的完整运行
# ---------------------------------------------------------------------------


async def run_agent(
    config: dict,
    run_mode: str = "dry-run",
    thread_id: Optional[str] = None,
    resume: bool = False,
) -> AgentState:
    """
    运行完整求职 Agent 流程（带 SQLite checkpoint 持久化）。

    Args:
        config:    完整配置字典（从 config.yaml 加载）
        run_mode:  运行模式（dry-run / semi-auto / full / crawl-only / filter-only / tailor-only）
        thread_id: Checkpoint 线程 ID，相同 ID 可从中断处恢复。
                   默认为当天日期（YYYYMMDD），每天一个 session。
        resume:    强制从 checkpoint 恢复（忽略 initial_state，从上次节点继续）。
                   若无 checkpoint 则退化为全新运行。

    Returns:
        最终 AgentState
    """
    if thread_id is None:
        thread_id = datetime.now().strftime("%Y%m%d")

    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as checkpointer:
        compiled = build_graph(checkpointer=checkpointer)
        thread_cfg = {"configurable": {"thread_id": thread_id}}

        # 判断是否有可恢复的 checkpoint
        checkpoint_tuple = await checkpointer.aget_tuple(thread_cfg["configurable"])
        has_checkpoint = (
            checkpoint_tuple is not None
            and checkpoint_tuple.metadata.get("step", -1) >= 0
        )

        if resume and has_checkpoint:
            logger.info(
                "[Orchestrator] 从 checkpoint 恢复 | thread_id=%s | 上次完成节点：%s",
                thread_id,
                checkpoint_tuple.metadata.get("source", "unknown"),
            )
            # 传 None 表示使用 checkpoint 中保存的状态继续执行
            final_state = await compiled.ainvoke(None, config=thread_cfg)
        else:
            if has_checkpoint and not resume:
                logger.info(
                    "[Orchestrator] 发现已有 checkpoint（thread_id=%s），全新运行将覆盖。"
                    "如需恢复，请传 resume=True。",
                    thread_id,
                )
            initial_state = make_initial_state(config, run_mode=run_mode)
            logger.info(
                "[Orchestrator] 全新运行 | thread_id=%s | run_mode=%s",
                thread_id, run_mode,
            )
            final_state = await compiled.ainvoke(initial_state, config=thread_cfg)

    _log_summary(final_state)
    return final_state


async def resume_agent(thread_id: str, config: dict) -> AgentState:
    """
    从指定 thread_id 的 checkpoint 恢复运行（便捷接口）。

    如果 checkpoint 不存在，抛出 RuntimeError。
    """
    CHECKPOINT_DB.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as checkpointer:
        thread_cfg = {"configurable": {"thread_id": thread_id}}
        checkpoint_tuple = await checkpointer.aget_tuple(thread_cfg["configurable"])
        if checkpoint_tuple is None:
            raise RuntimeError(
                f"未找到 thread_id={thread_id!r} 的 checkpoint，无法恢复。"
            )

        compiled = build_graph(checkpointer=checkpointer)
        logger.info("[Orchestrator] 恢复运行：thread_id=%s", thread_id)
        final_state = await compiled.ainvoke(None, config=thread_cfg)

    _log_summary(final_state)
    return final_state


# ---------------------------------------------------------------------------
# 不带 checkpoint 的简单运行（测试 / 快速调用用）
# ---------------------------------------------------------------------------


async def run_agent_in_memory(
    config: dict,
    run_mode: str = "dry-run",
) -> AgentState:
    """
    使用内存 checkpoint 运行（不写磁盘）。
    主要用于测试和开发调试，不支持中断恢复。
    """
    saver = MemorySaver()
    compiled = build_graph(checkpointer=saver)
    thread_cfg = {"configurable": {"thread_id": "in-memory"}}
    initial_state = make_initial_state(config, run_mode=run_mode)

    logger.info("[Orchestrator] 内存模式运行 | run_mode=%s", run_mode)
    final_state = await compiled.ainvoke(initial_state, config=thread_cfg)
    _log_summary(final_state)
    return final_state


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _log_summary(state: AgentState) -> None:
    """在运行结束时打印摘要日志。"""
    jobs_found = len(state.get("jobs_found", []))
    jobs_apply = len(state.get("jobs_to_apply", []))
    resumes = len(state.get("resumes_generated", []))
    applications = len(state.get("applications_sent", []))
    sent = sum(1 for a in state.get("applications_sent", []) if a.get("status") == "sent")
    errors = len(state.get("errors", []))

    logger.info(
        "[Orchestrator] 运行完成 ─ 抓取=%d | 达标=%d | 简历=%d | 投递=%d（已发送=%d）| 错误=%d | 阶段=%s",
        jobs_found, jobs_apply, resumes, applications, sent, errors,
        state.get("current_phase", "unknown"),
    )
