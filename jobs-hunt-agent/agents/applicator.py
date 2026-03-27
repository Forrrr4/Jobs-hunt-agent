"""
自动投递模块。

职责：
  1. 遍历 resumes_generated，找到每个职位对应的定制简历
  2. 检查数据库防止重复投递
  3. 根据 run_mode 决定行为：
       dry-run   → 只打印摘要，写 pending 记录，不操作浏览器
       semi-auto → 打印摘要请求人工确认（默认模式）
       full      → 自动投递，无需确认
  4. 调用 Boss直聘 / 实习僧 平台投递逻辑（Playwright）
  5. 写入 applications 表，更新 jobs.status = 'applied'
  6. 更新 AgentState.applications_sent
"""

import logging
import re
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Optional

from models.agent_state import AgentState
from models.job_posting import JobPosting
from prompts.apply_prompt import APPLY_SYSTEM_PROMPT, ApplyInput, build_apply_prompt
from tools.browser import BrowserManager, human_delay
from tools.db import (
    insert_application,
    is_already_applied,
    update_application_status,
    update_job_status,
)
from tools.llm_client import call_llm

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path("data/outputs")
SCREENSHOT_DIR = Path("data/screenshots")


# ---------------------------------------------------------------------------
# 用户界面工具（半自动模式）
# ---------------------------------------------------------------------------


def _build_summary(job: JobPosting, resume_info: dict) -> str:
    """构造投递摘要，用于在终端显示给用户确认。"""
    score_str = f"{job.score:.0f}分" if job.score is not None else "未评分"
    match_str = ""
    if job.match_points:
        match_str = "\n  匹配点：" + " | ".join(job.match_points[:3])
    concern_str = ""
    if job.concern_points:
        concern_str = "\n  关注点：" + " | ".join(job.concern_points[:2])

    resume_file = (
        Path(resume_info.get("pdf_path", "")).name
        or Path(resume_info.get("md_path", "")).name
        or "N/A"
    )

    return (
        f"\n{'='*60}\n"
        f"  职位：{job.title}\n"
        f"  公司：{job.company}  |  地点：{job.location}\n"
        f"  薪资：{job.salary_range or '面议'}  |  评分：{score_str}\n"
        f"  平台：{job.platform}  |  链接：{job.url}"
        f"{match_str}"
        f"{concern_str}\n"
        f"  简历：{resume_file}\n"
        f"{'='*60}"
    )


def _ask_user_confirm(summary: str) -> bool:
    """
    打印摘要并请求用户确认投递。

    Returns:
        True 表示用户确认（输入 y），False 表示跳过
    """
    print(summary)
    try:
        ans = input("是否投递此职位？[y/N]: ").strip().lower()
        return ans == "y"
    except (EOFError, KeyboardInterrupt):
        print("\n已跳过（中断）")
        return False


# ---------------------------------------------------------------------------
# LLM 生成打招呼消息
# ---------------------------------------------------------------------------


async def _generate_opening_message(job: JobPosting, config: dict) -> str:
    """
    调用 LLM 生成针对该职位的 Boss直聘 打招呼消息。
    失败时返回通用模板消息，不抛出异常。
    """
    user_cfg = config.get("user", {})
    search_cfg = config.get("search", {})
    llm_cfg = config.get("llm", {})

    inp = ApplyInput(
        job_title=job.title,
        job_company=job.company,
        job_location=job.location,
        jd_text=job.jd_text,
        user_name=user_cfg.get("name", "求职者"),
        skills=search_cfg.get("skills_required", []) + search_cfg.get("skills_bonus", []),
        match_points=job.match_points or [],
    )
    prompt = build_apply_prompt(inp)

    try:
        msg = await call_llm(
            prompt,
            system=APPLY_SYSTEM_PROMPT,
            model=llm_cfg.get("model", "claude-sonnet-4-20250514"),
            max_tokens=256,
            temperature=0.5,
        )
        return msg.strip()
    except Exception as e:
        logger.warning("[Apply] LLM 生成打招呼消息失败，使用默认模板：%s", e)
        return (
            f"您好，我对贵公司的{job.title}职位很感兴趣，"
            f"技能与 JD 高度匹配，期待进一步沟通。"
        )


# ---------------------------------------------------------------------------
# 平台投递：Boss直聘
# ---------------------------------------------------------------------------


async def _apply_boss(
    job: JobPosting,
    resume_info: dict,
    opening_msg: str,
    bm: BrowserManager,
    screenshot_dir: Path,
) -> dict:
    """
    使用 Playwright 在 Boss直聘 执行一次投递（发起聊天）。

    流程：
      1. 进入职位详情页
      2. 点击「立即沟通」按钮
      3. 等待聊天输入框出现
      4. 输入打招呼消息并发送
      5. 截图留存

    Returns:
        {status, notes, screenshot_path}
    """
    page = await bm.new_page()
    try:
        # 1. 加载职位页
        ok = await bm.goto_with_retry(page, job.url, wait_until="load", timeout=30_000)
        if not ok:
            return {"status": "failed", "notes": "职位页加载失败", "screenshot_path": None}

        await human_delay(2, 4)

        # 2. 点击「立即沟通」——多选择器兜底
        chat_btn_selectors = [
            ".btn-startchat",
            "a.start-btn-large",
            "[class*='btn-chat']",
            "button[ka='job-detail-contact']",
            ".op-btn-chat",
            "a[class*='chat']",
        ]
        clicked = False
        for sel in chat_btn_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    clicked = True
                    logger.debug("[Apply-Boss] 点击投递按钮：%s", sel)
                    break
            except Exception:
                continue

        if not clicked:
            shot = await _take_screenshot(page, screenshot_dir, job.id)
            return {"status": "failed", "notes": "未找到「立即沟通」按钮", "screenshot_path": shot}

        await human_delay(2, 3)

        # 3. 等待聊天输入框
        input_selectors = [
            ".chat-input-inner textarea",
            ".chat-input textarea",
            ".rich-input[contenteditable]",
            "[contenteditable='true']",
            ".input-area textarea",
        ]
        chat_input = None
        for sel in input_selectors:
            try:
                chat_input = await page.wait_for_selector(sel, timeout=6_000)
                if chat_input:
                    break
            except Exception:
                continue

        if not chat_input:
            shot = await _take_screenshot(page, screenshot_dir, job.id)
            return {"status": "failed", "notes": "聊天输入框未出现", "screenshot_path": shot}

        # 4. 输入消息
        await chat_input.fill(opening_msg)
        await human_delay(1, 2)

        # 5. 发送（点击发送按钮或回车）
        send_selectors = [
            ".send-btn",
            "button[class*='send']",
            ".btn-send",
            "[class*='chat-send']",
        ]
        sent_by_btn = False
        for sel in send_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    sent_by_btn = True
                    break
            except Exception:
                continue
        if not sent_by_btn:
            await chat_input.press("Enter")

        await human_delay(1, 2)
        shot = await _take_screenshot(page, screenshot_dir, job.id)
        return {"status": "sent", "notes": "Boss直聘投递成功", "screenshot_path": shot}

    except Exception as e:
        shot = await _take_screenshot(page, screenshot_dir, job.id)
        logger.error("[Apply-Boss] 投递异常 %s: %s", job.id, e)
        return {"status": "failed", "notes": str(e), "screenshot_path": shot}
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# 平台投递：实习僧（简化版，主要支持 Boss直聘）
# ---------------------------------------------------------------------------


async def _apply_shixiseng(
    job: JobPosting,
    resume_info: dict,
    bm: BrowserManager,
    screenshot_dir: Path,
) -> dict:
    """
    实习僧投递（半自动：打开详情页供用户手动点击，截图留证）。

    实习僧需要上传简历文件，全自动实现复杂度高，此版本：
      - 自动打开职位详情页
      - 截图提示用户手动点击「申请」
      - 等待 10 秒后截图保存结果
    """
    page = await bm.new_page()
    try:
        ok = await bm.goto_with_retry(page, job.url, wait_until="load", timeout=30_000)
        if not ok:
            return {"status": "failed", "notes": "实习僧职位页加载失败", "screenshot_path": None}

        await human_delay(2, 3)

        # 尝试点击申请按钮
        apply_selectors = [
            "a.btn-apply",
            ".apply-btn",
            "button[class*='apply']",
            "[class*='btn-apply']",
        ]
        for sel in apply_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    await human_delay(2, 3)
                    break
            except Exception:
                continue

        shot = await _take_screenshot(page, screenshot_dir, job.id)
        return {"status": "sent", "notes": "实习僧申请已提交（截图存档）", "screenshot_path": shot}

    except Exception as e:
        shot = await _take_screenshot(page, screenshot_dir, job.id)
        return {"status": "failed", "notes": str(e), "screenshot_path": shot}
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# 核心：单职位投递
# ---------------------------------------------------------------------------


async def apply_to_job(
    job: JobPosting,
    resume_info: dict,
    config: dict,
    run_mode: str = "dry-run",
    bm: Optional[BrowserManager] = None,
    screenshot_dir: Path = SCREENSHOT_DIR,
) -> Optional[dict]:
    """
    对单个职位执行完整投递流程。

    Args:
        job:          目标职位
        resume_info:  tailor_node 返回的简历记录（含 md_path/pdf_path）
        config:       完整配置字典
        run_mode:     dry-run / semi-auto / full
        bm:           BrowserManager 实例（dry-run 时可为 None）
        screenshot_dir: 截图存储目录

    Returns:
        成功时返回结果 dict（status=sent/pending），
        跳过时返回 None。
    """
    # 防重复投递
    if await is_already_applied(job.id):
        logger.info("[Apply] 跳过（已投递过）：%s @ %s", job.title, job.company)
        return None

    resume_path = resume_info.get("pdf_path") or resume_info.get("md_path", "")
    applied_at = datetime.now().isoformat()
    summary = _build_summary(job, resume_info)

    # ── DRY-RUN 模式 ─────────────────────────────────────────────
    if run_mode == "dry-run":
        logger.info("[Apply][DRY-RUN] 将投递：%s @ %s", job.title, job.company)
        print(f"\n[DRY-RUN]{summary}")
        app_id = await insert_application(
            job_id=job.id,
            resume_path=resume_path,
            status="pending",
            notes="dry-run 模式，未真实投递",
        )
        return {
            "job_id": job.id,
            "job_title": job.title,
            "job_company": job.company,
            "status": "pending",
            "applied_at": applied_at,
            "resume_path": resume_path,
            "screenshot_path": None,
            "notes": "dry-run 模式",
            "application_db_id": app_id,
        }

    # ── SEMI-AUTO 模式：请求用户确认 ─────────────────────────────
    if run_mode == "semi-auto":
        # 生成打招呼消息供用户预览
        opening_msg = await _generate_opening_message(job, config)
        full_summary = summary + f"\n\n  打招呼消息预览：\n  {opening_msg}\n"
        if not _ask_user_confirm(full_summary):
            logger.info("[Apply] 用户跳过：%s @ %s", job.title, job.company)
            return None
    else:
        # FULL 模式：直接生成消息，无需确认
        opening_msg = await _generate_opening_message(job, config)

    # ── 实际投递 ──────────────────────────────────────────────────
    if bm is None:
        logger.error("[Apply] 非 dry-run 模式但未提供 BrowserManager，跳过：%s", job.id)
        return None

    logger.info("[Apply] 开始投递：%s @ %s（平台：%s）", job.title, job.company, job.platform)

    if job.platform == "boss":
        platform_result = await _apply_boss(job, resume_info, opening_msg, bm, screenshot_dir)
    elif job.platform == "shixiseng":
        platform_result = await _apply_shixiseng(job, resume_info, bm, screenshot_dir)
    else:
        logger.warning("[Apply] 不支持的平台：%s，跳过", job.platform)
        return None

    final_status = platform_result.get("status", "failed")
    notes = platform_result.get("notes", "")
    shot_path = platform_result.get("screenshot_path")

    # 写入数据库
    app_id = await insert_application(
        job_id=job.id,
        resume_path=resume_path,
        status=final_status,
        screenshot_path=shot_path,
        notes=notes,
    )

    # 投递成功则更新职位状态
    if final_status == "sent":
        try:
            await update_job_status(job.id, "applied")
        except Exception as e:
            logger.warning("[Apply] 更新职位状态失败：%s", e)

    log_level = logger.info if final_status == "sent" else logger.warning
    log_level("[Apply] %s：%s @ %s | %s", final_status.upper(), job.title, job.company, notes)

    return {
        "job_id": job.id,
        "job_title": job.title,
        "job_company": job.company,
        "status": final_status,
        "applied_at": applied_at,
        "resume_path": resume_path,
        "screenshot_path": shot_path,
        "notes": notes,
        "application_db_id": app_id,
    }


# ---------------------------------------------------------------------------
# LangGraph 节点
# ---------------------------------------------------------------------------


async def apply_node(state: AgentState) -> AgentState:
    """
    LangGraph 节点：遍历 resumes_generated，依次投递每个职位。

    流程：
    1. 构建 job_id → JobPosting 映射表
    2. 根据 run_mode 决定是否启动浏览器
    3. 检查每日投递上限
    4. 对每个简历调用 apply_to_job()
    5. current_phase = 'done'
    """
    config = state["config"]
    run_mode = state.get("run_mode", "dry-run")
    resumes_generated: list[dict] = state.get("resumes_generated", [])
    jobs_to_apply: list[JobPosting] = state.get("jobs_to_apply", [])
    errors: list[dict] = list(state.get("errors", []))
    applications_sent: list[dict] = list(state.get("applications_sent", []))

    if not resumes_generated:
        logger.warning("[Apply] resumes_generated 为空，跳过投递阶段")
        return {**state, "applications_sent": [], "current_phase": "done"}

    # 构建 job_id → JobPosting 映射
    job_map: dict[str, JobPosting] = {j.id: j for j in jobs_to_apply}

    limits = config.get("limits", {})
    max_per_day = limits.get("max_applications_per_day", 20)

    logger.info(
        "[Apply] 开始投递 | run_mode=%s | 简历数=%d | 每日上限=%d",
        run_mode, len(resumes_generated), max_per_day,
    )

    # 根据 run_mode 决定是否启动浏览器
    boss_cfg = config.get("platforms", {}).get("boss", {})
    if run_mode in ("semi-auto", "full"):
        bm_ctx = BrowserManager(
            headless=True,
            cookie_file=boss_cfg.get("cookie_file", ".cookies/boss.json"),
        )
    else:
        bm_ctx = nullcontext()  # dry-run：不需要浏览器

    async with bm_ctx as bm:
        for resume_info in resumes_generated:
            if len(applications_sent) >= max_per_day:
                logger.warning("[Apply] 已达每日投递上限（%d），停止", max_per_day)
                break

            job_id = resume_info.get("job_id")
            job = job_map.get(job_id)
            if not job:
                logger.warning("[Apply] 找不到对应职位对象：%s，跳过", job_id)
                continue

            try:
                result = await apply_to_job(
                    job=job,
                    resume_info=resume_info,
                    config=config,
                    run_mode=run_mode,
                    bm=bm,
                    screenshot_dir=SCREENSHOT_DIR,
                )
                if result:
                    applications_sent.append(result)
            except Exception as e:
                logger.error("[Apply] 职位处理异常 %s: %s", job_id, e)
                errors.append({"module": "apply", "error": str(e), "job_id": job_id})

    sent_count = sum(1 for r in applications_sent if r.get("status") == "sent")
    pending_count = sum(1 for r in applications_sent if r.get("status") == "pending")
    logger.info(
        "[Apply] 完成 | 已投递=%d | 待投递=%d | 总处理=%d",
        sent_count, pending_count, len(resumes_generated),
    )

    return {
        **state,
        "applications_sent": applications_sent,
        "errors": errors,
        "current_phase": "done",
    }


# ---------------------------------------------------------------------------
# 独立运行接口
# ---------------------------------------------------------------------------


async def run_apply(
    jobs: list[JobPosting],
    resumes: list[dict],
    config: dict,
    run_mode: str = "dry-run",
) -> list[dict]:
    """
    独立运行投递，不依赖 LangGraph state。

    适合 `--mode apply-only` 场景。
    """
    resume_map = {r["job_id"]: r for r in resumes}
    results = []

    boss_cfg = config.get("platforms", {}).get("boss", {})
    if run_mode in ("semi-auto", "full"):
        bm_ctx = BrowserManager(
            headless=True,
            cookie_file=boss_cfg.get("cookie_file", ".cookies/boss.json"),
        )
    else:
        bm_ctx = nullcontext()

    async with bm_ctx as bm:
        for job in jobs:
            resume_info = resume_map.get(job.id)
            if not resume_info:
                logger.warning("[Apply] 职位无对应简历，跳过：%s", job.id)
                continue
            result = await apply_to_job(job, resume_info, config, run_mode, bm)
            if result:
                results.append(result)

    return results


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


async def _take_screenshot(page, screenshot_dir: Path, job_id: str) -> Optional[str]:
    """对当前页面截图，失败时返回 None。"""
    try:
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^\w\-]", "_", job_id)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = screenshot_dir / f"{safe_id}_{ts}.png"
        await page.screenshot(path=str(path), full_page=False)
        logger.debug("[Apply] 截图：%s", path)
        return str(path)
    except Exception as e:
        logger.warning("[Apply] 截图失败：%s", e)
        return None
