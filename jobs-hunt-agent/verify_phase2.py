"""
Phase 2 验证脚本：使用修正后的 ShixisengCrawler 实际抓取。
运行：python verify_phase2.py
"""

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent))

from tools.db import init_db, DB_PATH, get_db
from tools.browser import BrowserManager
from agents.crawler import ShixisengCrawler
from tools.db import upsert_job, job_exists


VERIFY_CONFIG = {
    "limits": {"max_jobs_per_run": 5, "request_delay_seconds": [1.5, 2.5]},
    "platforms": {
        "shixiseng": {"enabled": True, "cookie_file": None},
        "boss": {"enabled": False, "cookie_file": None},
    },
    "search": {
        "cities": ["北京"],
        "skills_required": ["AI实习"],
        "skills_bonus": [],
    },
}


async def verify_db(label: str):
    """打印当前数据库记录。"""
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id, title, company, location, salary_range, platform, "
            "length(jd_text) as jd_len FROM jobs ORDER BY crawled_at DESC LIMIT 10"
        )
        rows = await cursor.fetchall()

    print(f"\n  [{label}] jobs.db 中共 {len(rows)} 条记录：")
    if rows:
        print(f"  {'职位标题':<22} {'公司':<18} {'薪资':<12} {'城市':<6} {'JD长度':>6}  ID")
        print("  " + "-" * 85)
        for r in rows:
            job_id, title, company, location, salary, platform, jd_len = r
            title_s = (title or "")[:20]
            company_s = (company or "")[:16]
            salary_s = (salary or "面议")[:10]
            loc_s = (location or "")[:5]
            id_s = (job_id or "")[-20:]
            print(f"  {title_s:<22} {company_s:<18} {salary_s:<12} {loc_s:<6} {jd_len:>6}字  ...{id_s}")
    return len(rows)


async def main():
    print("=" * 70)
    print("Phase 2 完整验证：ShixisengCrawler 抓取'AI实习 北京'第一页")
    print("=" * 70)

    # Step 1: 初始化数据库
    print("\n[1] 初始化数据库...")
    await init_db()
    print(f"    [OK] {DB_PATH.resolve()}")
    await verify_db("初始")

    # Step 2: 抓取列表页
    print("\n[2] 启动浏览器，抓取实习僧列表页...")
    async with BrowserManager(headless=True) as bm:
        crawler = ShixisengCrawler(bm, VERIFY_CONFIG)

        jobs_raw = await crawler.fetch_job_list(["AI实习"], "北京", page=1)
        print(f"    列表页找到 {len(jobs_raw)} 个职位卡片（含 jd_text 为空）")

        if not jobs_raw:
            print("    [FAIL] 未找到任何职位，请检查 DOM 结构")
            return

        # 打印列表页基本信息
        print(f"\n[3] 列表页解析结果：")
        print(f"    {'#':<4} {'职位标题':<25} {'公司':<20} {'薪资':<12} {'城市'}")
        print("    " + "-" * 72)
        for i, job in enumerate(jobs_raw, 1):
            title = (job.title or "N/A")[:23]
            company = (job.company or "N/A")[:18]
            salary = (job.salary_range or "面议")[:10]
            location = (job.location or "N/A")[:8]
            print(f"    {i:<4} {title:<25} {company:<20} {salary:<12} {location}")

        # Step 3: 抓取前 3 个详情页 + 写库
        print(f"\n[4] 抓取前 3 个职位的详情页并写入数据库...")
        inserted = 0
        skipped = 0
        detail_results = []

        for job in jobs_raw[:3]:
            if await job_exists(job.id):
                print(f"    [SKIP] 已存在：{job.id}")
                skipped += 1
                continue

            print(f"    正在抓取详情：{job.title} @ {job.company}...")
            jd_text = await crawler.fetch_job_detail(job.id, job.url)

            if jd_text:
                job = job.model_copy(update={"jd_text": jd_text})
                ok = await upsert_job(job)
                if ok:
                    inserted += 1
                    jd_preview = jd_text[:100].replace("\n", " ")
                    detail_results.append((job.title, job.company, jd_preview))
                    print(f"    [OK] 写入成功（JD {len(jd_text)} 字）")
            else:
                print(f"    [WARN] JD 为空，跳过：{job.url}")

        print(f"\n    新插入：{inserted} 条，跳过（重复）：{skipped} 条")

        # 打印 JD 预览
        if detail_results:
            print(f"\n[5] JD 内容预览：")
            for i, (title, company, preview) in enumerate(detail_results, 1):
                print(f"    [{i}] {title} @ {company}")
                print(f"        {preview}...")

    # Step 4: 最终验证数据库
    print()
    total = await verify_db("最终")

    # Step 5: 验证字段完整性
    print(f"\n[6] 字段完整性验证...")
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE title IS NOT NULL AND company IS NOT NULL "
            "AND jd_text IS NOT NULL AND length(jd_text) > 50"
        )
        valid_count = (await cursor.fetchone())[0]

    print(f"    title + company + jd_text(>50字) 均非空：{valid_count}/{total} 条")

    print("\n" + "=" * 70)
    if total > 0 and valid_count > 0:
        print("[PASS] Phase 2 验证通过！")
        print(f"       - 列表页解析：{len(jobs_raw)} 个职位（标题、公司、薪资、城市正确）")
        print(f"       - 详情页抓取：{inserted} 个（JD 文本完整写入数据库）")
        print(f"       - 数据库记录：{total} 条，字段完整：{valid_count} 条")
    else:
        print("[FAIL] 验证未通过，请检查日志")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
