"""
智能筛选模块。

职责：
  1. 从数据库读取待筛选职位（status='new'）
  2. 对每个 JD 调用 Claude 打分（0-100），并发数由 Semaphore 控制
  3. 将评分结果写回数据库（score / score_reason / match_points / concern_points）
  4. 筛选出 score >= threshold 的职位，更新 AgentState.jobs_filtered

评分维度（见 prompts/filter_prompt.py）：
  - 技能匹配度（40 分）
  - 发展潜力（30 分）
  - 综合条件（30 分）
"""

import asyncio
import logging
from typing import Optional

from models.agent_state import AgentState
from models.job_posting import JobPosting
from prompts.filter_prompt import (
    FILTER_SYSTEM_PROMPT,
    FilterInput,
    build_filter_prompt,
)
from tools.db import (
    get_jobs_by_status,
    get_top_jobs,
    update_job_score,
    update_job_status,
)
from tools.llm_client import call_llm_json

logger = logging.getLogger(__name__)

# 并发上限：同时最多 N 个 LLM 请求，避免触发速率限制
_DEFAULT_CONCURRENCY = 3


# ---------------------------------------------------------------------------
# 核心：单职位评分
# ---------------------------------------------------------------------------


async def score_job(
    job: JobPosting,
    search_config: dict,
    semaphore: asyncio.Semaphore,
    llm_config: dict,
) -> Optional[dict]:
    """
    对单个职位调用 LLM 评分。

    Args:
        job:           待评分职位
        search_config: config.yaml 中的 search 节
        semaphore:     并发控制信号量
        llm_config:    config.yaml 中的 llm 节

    Returns:
        评分结果 dict（含 score/reason/match_points/concern_points），
        或 None（调用失败）
    """
    async with semaphore:
        inp = FilterInput(
            job_id=job.id,
            title=job.title,
            company=job.company,
            location=job.location,
            salary_range=job.salary_range or "面议",
            platform=job.platform,
            jd_text=job.jd_text,
            cities=search_config.get("cities", []),
            skills_required=search_config.get("skills_required", []),
            skills_bonus=search_config.get("skills_bonus", []),
            salary_min=search_config.get("salary_min", 0),
            industries=search_config.get("industries", []),
            job_types=search_config.get("job_types", []),
        )

        prompt = build_filter_prompt(inp)

        logger.info("[Filter] 评分中：%s @ %s", job.title, job.company)

        # 调用 LLM，JSON 解析失败时返回 fallback（不中断整体流程）
        result = await call_llm_json(
            prompt,
            system=FILTER_SYSTEM_PROMPT,
            model=llm_config.get("model", "claude-sonnet-4-20250514"),
            max_tokens=llm_config.get("max_tokens", 1024),
            temperature=llm_config.get("temperature", 0.2),
            fallback=None,
        )

        if result is None:
            logger.error("[Filter] LLM 返回无法解析，跳过：%s", job.id)
            return None

        # 校验必要字段
        score = result.get("score")
        if score is None or not isinstance(score, (int, float)):
            logger.error("[Filter] 返回 JSON 缺少 score 字段：%s | %s", job.id, result)
            return None

        # score 范围钳制
        score = max(0.0, min(100.0, float(score)))
        result["score"] = score
        result["job_id"] = job.id
        return result


# ---------------------------------------------------------------------------
# 批量评分 + 数据库写回
# ---------------------------------------------------------------------------


async def batch_score_jobs(
    jobs: list[JobPosting],
    search_config: dict,
    llm_config: dict,
    concurrency: int = _DEFAULT_CONCURRENCY,
) -> list[dict]:
    """
    并发评分一批职位，结果写回数据库。

    Args:
        jobs:          待评分职位列表
        search_config: 搜索偏好配置
        llm_config:    LLM 配置
        concurrency:   最大并发数

    Returns:
        成功评分的结果列表（每项含 job_id / score / reason / match_points / concern_points）
    """
    if not jobs:
        return []

    semaphore = asyncio.Semaphore(concurrency)
    tasks = [score_job(job, search_config, semaphore, llm_config) for job in jobs]

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    successful: list[dict] = []
    for job, result in zip(jobs, raw_results):
        if isinstance(result, Exception):
            logger.error("[Filter] 评分异常 %s: %s", job.id, result)
            continue
        if result is None:
            continue

        # 写回数据库
        try:
            await update_job_score(
                job_id=job.id,
                score=result["score"],
                score_reason=result.get("reason", ""),
                match_points=result.get("match_points"),
                concern_points=result.get("concern_points"),
            )
            logger.info(
                "[Filter] 评分完成：%s @ %s → %.0f 分",
                job.title, job.company, result["score"],
            )
            successful.append(result)
        except Exception as e:
            logger.error("[Filter] 写回数据库失败 %s: %s", job.id, e)

    return successful


# ---------------------------------------------------------------------------
# LangGraph 节点
# ---------------------------------------------------------------------------


async def filter_node(state: AgentState) -> AgentState:
    """
    LangGraph 节点：对所有未评分职位进行 LLM 评分，筛选出达标职位。

    流程：
    1. 从 state.jobs_found 取待评分列表（jd_text 非空且 score 为 None 的）
    2. 批量调用 Claude 评分，并发数 = min(3, len(jobs))
    3. 结果写回数据库
    4. 筛选 score >= threshold 的职位 → state.jobs_filtered
    5. 进一步筛选 → state.jobs_to_apply

    Args:
        state: 当前 AgentState

    Returns:
        更新后的 AgentState（jobs_filtered / jobs_to_apply / errors / current_phase）
    """
    config = state["config"]
    search_cfg = config.get("search", {})
    llm_cfg = config.get("llm", {})
    threshold: float = float(search_cfg.get("filter_score_threshold", 65))
    errors: list[dict] = list(state.get("errors", []))

    # 取 jobs_found 中尚未评分的职位（jd_text 非空）
    jobs_to_score: list[JobPosting] = [
        j for j in state.get("jobs_found", [])
        if j.jd_text and j.score is None
    ]

    # 如果 jobs_found 为空，尝试直接从数据库读取
    if not jobs_to_score:
        logger.info("[Filter] jobs_found 为空，从数据库读取 status=new 的职位")
        jobs_to_score = await get_jobs_by_status("new")
        jobs_to_score = [j for j in jobs_to_score if j.jd_text]

    logger.info("[Filter] 待评分职位：%d 个（阈值 %.0f 分）", len(jobs_to_score), threshold)

    if not jobs_to_score:
        logger.warning("[Filter] 无待评分职位，跳过筛选阶段")
        return {
            **state,
            "jobs_filtered": [],
            "jobs_to_apply": [],
            "current_phase": "tailor",
        }

    # 批量评分
    try:
        score_results = await batch_score_jobs(
            jobs_to_score,
            search_config=search_cfg,
            llm_config=llm_cfg,
        )
    except Exception as e:
        logger.error("[Filter] 批量评分整体异常：%s", e)
        errors.append({"module": "filter", "error": str(e), "job_id": None})
        score_results = []

    # 从数据库读取已更新评分的职位（含 score / match_points 等）
    all_scored: list[JobPosting] = await get_top_jobs(n=1000, min_score=0)

    # 筛选达标职位
    jobs_filtered = [j for j in all_scored if j.score is not None]
    jobs_to_apply = [j for j in jobs_filtered if j.score >= threshold]

    logger.info(
        "[Filter] 完成 | 已评分：%d 个 | 达标（≥%.0f）：%d 个",
        len(jobs_filtered), threshold, len(jobs_to_apply),
    )

    return {
        **state,
        "jobs_filtered": jobs_filtered,
        "jobs_to_apply": jobs_to_apply,
        "errors": errors,
        "current_phase": "tailor",
    }


# ---------------------------------------------------------------------------
# 便捷接口：直接从数据库批量筛选（不依赖 LangGraph state）
# ---------------------------------------------------------------------------


async def run_filter(config: dict) -> list[JobPosting]:
    """
    独立运行筛选模块，从数据库取所有 status=new 的职位并评分。

    适合在 main.py --mode filter-only 场景下直接调用。

    Returns:
        达到阈值的 JobPosting 列表（已按 score 降序排列）
    """
    search_cfg = config.get("search", {})
    llm_cfg = config.get("llm", {})
    threshold = float(search_cfg.get("filter_score_threshold", 65))

    jobs = await get_jobs_by_status("new")
    jobs = [j for j in jobs if j.jd_text]

    if not jobs:
        logger.warning("[Filter] 数据库中无待筛选职位")
        return []

    await batch_score_jobs(jobs, search_cfg, llm_cfg)

    # 重新读取（已含评分）
    return await get_top_jobs(n=1000, min_score=threshold)
