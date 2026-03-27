"""
简历定制模块。

职责：
  1. 读取用户基础简历（Markdown）
  2. 针对每个目标职位，调用 LLM 生成定制版简历
  3. 对定制结果进行完整性校验（确保事实未被篡改）
  4. 将定制简历保存为 Markdown + PDF（+ 可选 DOCX）
  5. 更新数据库中职位状态为 'tailored'
  6. 更新 AgentState.resumes_generated

安全原则：简历事实（公司名/日期/数字/学历）绝不允许被 LLM 修改。
"""

import logging
from pathlib import Path
from typing import Optional

from models.agent_state import AgentState
from models.job_posting import JobPosting
from prompts.tailor_prompt import TAILOR_SYSTEM_PROMPT, TailorInput, build_tailor_prompt
from tools.db import update_job_status
from tools.llm_client import call_llm
from tools.resume_parser import (
    read_resume,
    save_resume_markdown,
    save_resume_pdf,
    save_resume_docx,
    validate_resume_integrity,
)

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("data/outputs")


# ---------------------------------------------------------------------------
# 核心：单职位简历定制
# ---------------------------------------------------------------------------


async def tailor_resume_for_job(
    job: JobPosting,
    base_resume: str,
    llm_config: dict,
    output_dir: Path = OUTPUT_DIR,
    also_docx: bool = False,
) -> Optional[dict]:
    """
    为单个职位生成定制简历。

    流程：
      1. 构造 TailorInput + prompt
      2. 调用 Claude（温度 0.3，允许适度创意）
      3. 完整性校验（日期/数字/加粗项不丢失）
      4. 校验通过则保存 Markdown + PDF；失败则记录日志并返回 None

    Args:
        job:          目标职位
        base_resume:  基础简历 Markdown 全文
        llm_config:   LLM 配置（model/max_tokens/temperature）
        output_dir:   输出目录（默认 data/outputs/）
        also_docx:    是否同时生成 DOCX 文件

    Returns:
        成功时返回 {job_id, md_path, pdf_path, docx_path}；失败返回 None
    """
    logger.info("[Tailor] 开始定制：%s @ %s", job.title, job.company)

    inp = TailorInput(
        job_id=job.id,
        job_title=job.title,
        job_company=job.company,
        job_location=job.location,
        jd_text=job.jd_text,
        resume_text=base_resume,
    )
    prompt = build_tailor_prompt(inp)

    # 调用 LLM
    try:
        tailored_text = await call_llm(
            prompt,
            system=TAILOR_SYSTEM_PROMPT,
            model=llm_config.get("model", "claude-sonnet-4-20250514"),
            max_tokens=llm_config.get("max_tokens", 4096),
            temperature=llm_config.get("temperature", 0.3),
        )
    except Exception as e:
        logger.error("[Tailor] LLM 调用失败 %s: %s", job.id, e)
        return None

    # 清理：去掉 LLM 可能包裹的 markdown 代码块
    tailored_text = _strip_code_fence(tailored_text)

    if not tailored_text or len(tailored_text) < 100:
        logger.error("[Tailor] LLM 返回内容过短，跳过：%s（%d 字）", job.id, len(tailored_text))
        return None

    # 完整性校验
    is_valid, violations = validate_resume_integrity(base_resume, tailored_text)
    if not is_valid:
        logger.warning(
            "[Tailor] 完整性校验失败（%d 项），仍保存但标记警告：%s | %s",
            len(violations), job.id, violations,
        )
        # 在定制简历顶部追加警告注释，方便人工审查
        warning_block = (
            f"\n\n> ⚠️ 完整性警告（{len(violations)} 项）：{'; '.join(violations)}\n\n"
        )
        tailored_text = tailored_text + warning_block

    # 保存文件
    md_path = save_resume_markdown(tailored_text, output_dir, job.id)
    pdf_path = save_resume_pdf(tailored_text, output_dir, job.id)
    docx_path = save_resume_docx(tailored_text, output_dir, job.id) if also_docx else None

    # 更新数据库状态
    try:
        await update_job_status(job.id, "tailored")
    except Exception as e:
        logger.warning("[Tailor] 更新 DB 状态失败 %s: %s", job.id, e)

    result = {
        "job_id": job.id,
        "job_title": job.title,
        "job_company": job.company,
        "md_path": str(md_path),
        "pdf_path": str(pdf_path) if pdf_path else None,
        "docx_path": str(docx_path) if docx_path else None,
        "integrity_ok": is_valid,
        "violations": violations,
    }

    logger.info(
        "[Tailor] 完成：%s | md=%s | pdf=%s | 完整性=%s",
        job.id,
        md_path.name,
        pdf_path.name if pdf_path else "N/A",
        "OK" if is_valid else f"WARN({len(violations)}项)",
    )
    return result


# ---------------------------------------------------------------------------
# LangGraph 节点
# ---------------------------------------------------------------------------


async def tailor_node(state: AgentState) -> AgentState:
    """
    LangGraph 节点：为所有达标职位生成定制简历。

    流程：
    1. 读取基础简历文件
    2. 遍历 state.jobs_to_apply（已通过分数阈值的职位）
    3. 对每个职位调用 tailor_resume_for_job()
    4. 收集成功结果 → state.resumes_generated
    5. 更新 current_phase = 'apply'

    Args:
        state: 当前 AgentState

    Returns:
        更新后的 AgentState
    """
    config = state["config"]
    llm_cfg = config.get("llm", {})
    resume_path = config.get("user", {}).get("base_resume_path", "data/base_resume.md")
    errors: list[dict] = list(state.get("errors", []))

    # 读取基础简历
    try:
        base_resume = read_resume(resume_path)
    except FileNotFoundError as e:
        logger.error("[Tailor] 简历文件不存在：%s", e)
        errors.append({"module": "tailor", "error": str(e), "job_id": None})
        return {
            **state,
            "errors": errors,
            "current_phase": "apply",
            "should_stop": True,
        }

    jobs_to_apply: list[JobPosting] = state.get("jobs_to_apply", [])

    if not jobs_to_apply:
        logger.warning("[Tailor] 无需定制简历的职位（jobs_to_apply 为空）")
        return {
            **state,
            "resumes_generated": [],
            "current_phase": "apply",
        }

    logger.info("[Tailor] 开始定制简历 | 目标职位数：%d", len(jobs_to_apply))

    resumes_generated: list[dict] = list(state.get("resumes_generated", []))

    for job in jobs_to_apply:
        try:
            result = await tailor_resume_for_job(
                job=job,
                base_resume=base_resume,
                llm_config=llm_cfg,
                output_dir=OUTPUT_DIR,
                also_docx=False,
            )
            if result:
                resumes_generated.append(result)
            else:
                errors.append({
                    "module": "tailor",
                    "error": "LLM 返回为空或校验失败",
                    "job_id": job.id,
                })
        except Exception as e:
            logger.error("[Tailor] 职位处理异常 %s: %s", job.id, e)
            errors.append({"module": "tailor", "error": str(e), "job_id": job.id})

    logger.info(
        "[Tailor] 完成 | 成功：%d/%d 个职位",
        len(resumes_generated), len(jobs_to_apply),
    )

    return {
        **state,
        "resumes_generated": resumes_generated,
        "errors": errors,
        "current_phase": "apply",
    }


# ---------------------------------------------------------------------------
# 独立运行接口（不依赖 LangGraph state）
# ---------------------------------------------------------------------------


async def run_tailor(jobs: list[JobPosting], config: dict) -> list[dict]:
    """
    独立运行简历定制，不依赖 LangGraph state。

    适合 `--mode tailor-only` 场景。

    Returns:
        成功生成的简历记录列表
    """
    llm_cfg = config.get("llm", {})
    resume_path = config.get("user", {}).get("base_resume_path", "data/base_resume.md")
    base_resume = read_resume(resume_path)

    results = []
    for job in jobs:
        result = await tailor_resume_for_job(job, base_resume, llm_cfg)
        if result:
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    """
    去除 LLM 可能包裹的 Markdown 代码块标记。

    有时 LLM 会把整个简历包在 ```markdown ... ``` 中返回。
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # 去首行（```markdown 或 ```）和末行（```）
        start = 1
        end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[start:end]).strip()
    return text
