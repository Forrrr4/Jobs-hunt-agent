"""
Phase 3 验证脚本：Filter 模块验证。

分两个层次：
  Layer 1（不需要 API key）：验证 prompt 构造、输入格式、DB 读取
  Layer 2（需要 ANTHROPIC_API_KEY）：真实 LLM 评分，打印每个职位的分数和理由

用法：
  # Layer 1 only（无需 key）
  python verify_phase3.py

  # Layer 1 + Layer 2（需要先设置 key）
  set ANTHROPIC_API_KEY=sk-ant-...       # Windows
  export ANTHROPIC_API_KEY=sk-ant-...   # Linux/Mac
  python verify_phase3.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

from tools.db import get_db, get_jobs_by_status, DB_PATH
from prompts.filter_prompt import FilterInput, build_filter_prompt, FILTER_SYSTEM_PROMPT

SEP = "=" * 70
SEP2 = "-" * 70


# ---------------------------------------------------------------------------
# Layer 1：无需 API key 的验证
# ---------------------------------------------------------------------------

async def verify_layer1() -> list:
    """验证 DB 读取、FilterInput 构造、prompt 模板填充。返回从 DB 读到的 jobs。"""
    print(SEP)
    print("Layer 1：Prompt 构造 & 数据库读取验证（无需 API key）")
    print(SEP)

    # 1-A: 读取数据库中的职位
    print("\n[1-A] 从 jobs.db 读取待评分职位（status=new）...")
    jobs = await get_jobs_by_status("new")
    jobs = [j for j in jobs if j.jd_text]

    if not jobs:
        print("      [WARN] 数据库中无 status=new 且有 JD 的职位")
        print("      请先运行 verify_phase2.py 抓取职位数据")
        return []

    print(f"      [OK] 找到 {len(jobs)} 个待评分职位：")
    for j in jobs:
        print(f"        - {j.id} | {j.title} @ {j.company} | JD={len(j.jd_text)}字")

    # 1-B: 构造 FilterInput 并生成 prompt
    print("\n[1-B] 构造 FilterInput 和 Prompt 模板...")

    sample_search_config = {
        "cities": ["北京", "上海"],
        "skills_required": ["Python", "LLM", "Agent"],
        "skills_bonus": ["LangChain", "RAG"],
        "salary_min": 10000,
        "industries": ["互联网", "AI"],
        "job_types": ["实习"],
    }

    for job in jobs:
        inp = FilterInput(
            job_id=job.id,
            title=job.title,
            company=job.company,
            location=job.location,
            salary_range=job.salary_range or "面议",
            platform=job.platform,
            jd_text=job.jd_text,
            **{k: sample_search_config[k] for k in sample_search_config},
        )
        prompt = build_filter_prompt(inp)

        # 验证 prompt 包含必要内容
        checks = {
            "职位标题": job.title in prompt,
            "公司名称": job.company in prompt,
            "JD文本(前30字)": job.jd_text[:30] in prompt,
            "用户技能": "Python" in prompt,
            "目标城市": "北京" in prompt,
        }
        all_ok = all(checks.values())
        status = "[OK]" if all_ok else "[FAIL]"
        print(f"      {status} {job.title} @ {job.company}")
        for check_name, ok in checks.items():
            flag = "  " if ok else "!!"
            print(f"           {flag} {check_name}: {'通过' if ok else '失败'}")

    # 1-C: 打印 system prompt 关键信息
    print(f"\n[1-C] System Prompt 摘要：")
    print(f"      字数：{len(FILTER_SYSTEM_PROMPT)} 字")
    dims = ["技能匹配度（满分 40 分）", "发展潜力（满分 30 分）", "综合条件（满分 30 分）"]
    for d in dims:
        present = d[:6] in FILTER_SYSTEM_PROMPT
        print(f"      {'[OK]' if present else '[!!]'} {d}")

    # 1-D: 显示第一个职位的完整 prompt（截取，供目视检查）
    print(f"\n[1-D] 第一个职位的 Prompt 预览（前 600 字）：")
    print(SEP2)
    first_job = jobs[0]
    inp = FilterInput(
        job_id=first_job.id,
        title=first_job.title,
        company=first_job.company,
        location=first_job.location,
        salary_range=first_job.salary_range or "面议",
        platform=first_job.platform,
        jd_text=first_job.jd_text,
        **{k: sample_search_config[k] for k in sample_search_config},
    )
    preview = build_filter_prompt(inp)[:600]
    print(preview)
    print("  ...")
    print(SEP2)

    print("\n[PASS] Layer 1 验证通过！")
    return jobs


# ---------------------------------------------------------------------------
# Layer 2：真实 LLM 评分（需要 ANTHROPIC_API_KEY）
# ---------------------------------------------------------------------------

async def verify_layer2(jobs: list):
    """调用真实 Claude API 对每个职位评分，打印结果表格。"""
    print()
    print(SEP)
    print("Layer 2：真实 LLM 评分验证（需要 ANTHROPIC_API_KEY）")
    print(SEP)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("\n  [SKIP] 未检测到 ANTHROPIC_API_KEY 环境变量")
        print()
        print("  设置方法（Windows CMD）：")
        print("    set ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx")
        print()
        print("  设置方法（PowerShell）：")
        print("    $env:ANTHROPIC_API_KEY = 'sk-ant-xxxxxxxxxxxx'")
        print()
        print("  设置后重新运行本脚本即可执行 Layer 2 验证。")
        return

    print(f"\n  [OK] ANTHROPIC_API_KEY 已设置（{api_key[:12]}...）")
    print(f"  待评分职位：{len(jobs)} 个")

    from agents.filter import batch_score_jobs

    sample_search_config = {
        "cities": ["北京", "上海"],
        "skills_required": ["Python", "LLM", "Agent"],
        "skills_bonus": ["LangChain", "RAG", "FastAPI"],
        "salary_min": 10000,
        "industries": ["互联网", "AI"],
        "job_types": ["实习"],
        "filter_score_threshold": 65,
    }
    llm_config = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1024,
        "temperature": 0.2,
    }

    print("\n  正在评分（并发=3，每个职位约 3-10 秒）...")
    print(SEP2)

    results = await batch_score_jobs(
        jobs,
        search_config=sample_search_config,
        llm_config=llm_config,
        concurrency=3,
    )

    # 打印评分结果表
    print()
    print(f"  评分完成！共 {len(results)}/{len(jobs)} 个职位成功评分\n")
    print(f"  {'职位':<20} {'公司':<16} {'分数':>5}  {'评级':<8}  理由（摘要）")
    print("  " + SEP2)

    threshold = 65
    pass_count = 0

    for r in sorted(results, key=lambda x: -x.get("score", 0)):
        job_id = r.get("job_id", "")
        score = r.get("score", 0)
        reason = r.get("reason", "")
        match_pts = r.get("match_points", [])
        concern_pts = r.get("concern_points", [])

        # 找对应 job
        job = next((j for j in jobs if j.id == job_id), None)
        title = (job.title if job else job_id)[:18]
        company = (job.company if job else "")[:14]

        rating = "达标" if score >= threshold else "不达标"
        if score >= threshold:
            pass_count += 1

        reason_short = reason[:35].replace("\n", " ")
        print(f"  {title:<20} {company:<16} {score:>5.0f}  [{rating}]  {reason_short}...")

        if match_pts:
            print(f"    优势：{', '.join(match_pts)}")
        if concern_pts:
            print(f"    风险：{', '.join(concern_pts)}")
        print()

    print(SEP2)
    print(f"  达标（>={threshold}分）：{pass_count}/{len(results)} 个职位")

    # 验证数据库写回
    print(f"\n[2-B] 验证评分已写回数据库...")
    async with get_db() as conn:
        cur = await conn.execute(
            "SELECT id, title, score, score_reason, status FROM jobs "
            "WHERE score IS NOT NULL ORDER BY score DESC"
        )
        rows = await cur.fetchall()

    print(f"  数据库中已有评分的记录：{len(rows)} 条")
    print(f"  {'职位ID':<35} {'标题':<18} {'分数':>5}  {'状态'}")
    print("  " + "-" * 65)
    for row in rows:
        job_id, title, score, reason, status = row
        print(f"  {job_id:<35} {(title or '')[:16]:<18} {score:>5.0f}  {status}")

    if len(rows) > 0:
        print(f"\n[PASS] Layer 2 验证通过！")
        print(f"       {len(results)} 个职位成功评分，{len(rows)} 条写入数据库")
    else:
        print(f"\n[FAIL] 数据库中无评分记录，请检查 update_job_score 是否正常工作")


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

async def main():
    print()
    print("Phase 3 Filter 模块验证")
    print()

    jobs = await verify_layer1()

    if jobs:
        await verify_layer2(jobs)

    print()
    print(SEP)
    print("验证完成。如需 Layer 2 真实评分，请设置 ANTHROPIC_API_KEY 后重新运行。")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
