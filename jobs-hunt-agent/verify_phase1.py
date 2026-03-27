"""
Phase 1 验证脚本：确认数据库初始化正确。
运行：python verify_phase1.py
"""

import asyncio
import sys
from pathlib import Path

# 强制 UTF-8 输出（兼容 Windows）
sys.stdout.reconfigure(encoding="utf-8")

# 确保从项目根目录 import
sys.path.insert(0, str(Path(__file__).parent))

import aiosqlite
from tools.db import init_db, DB_PATH


async def main():
    print("=" * 60)
    print("Phase 1 验证：数据库初始化")
    print("=" * 60)

    # Step 1: 初始化数据库
    print("\n[1] 运行 init_db()...")
    await init_db()

    if DB_PATH.exists():
        print(f"    [OK] 数据库文件已创建：{DB_PATH.resolve()}")
        print(f"    文件大小：{DB_PATH.stat().st_size} bytes")
    else:
        print(f"    [FAIL] 数据库文件未创建：{DB_PATH}")
        return

    # Step 2: 查看表结构
    print("\n[2] 检查表结构...")
    async with aiosqlite.connect(DB_PATH) as conn:
        # 列出所有表
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        print(f"    已创建的表：{tables}")

        expected = {"jobs", "applications"}
        missing = expected - set(tables)
        if missing:
            print(f"    [FAIL] 缺少表：{missing}")
            return
        else:
            print("    [OK] jobs 表和 applications 表均已创建")

        # Step 3: 打印 jobs 表结构
        print("\n[3] jobs 表结构（PRAGMA table_info）：")
        cursor = await conn.execute("PRAGMA table_info(jobs)")
        rows = await cursor.fetchall()
        print(f"    {'cid':<4} {'name':<18} {'type':<10} {'notnull':<8} {'default'}")
        print("    " + "-" * 58)
        for row in rows:
            cid, name, col_type, notnull, default, pk = row
            print(f"    {cid:<4} {name:<18} {col_type:<10} {notnull:<8} {default or ''}")

        # Step 4: 打印 applications 表结构
        print("\n[4] applications 表结构（PRAGMA table_info）：")
        cursor = await conn.execute("PRAGMA table_info(applications)")
        rows = await cursor.fetchall()
        print(f"    {'cid':<4} {'name':<18} {'type':<10} {'notnull':<8} {'default'}")
        print("    " + "-" * 58)
        for row in rows:
            cid, name, col_type, notnull, default, pk = row
            print(f"    {cid:<4} {name:<18} {col_type:<10} {notnull:<8} {default or ''}")

        # Step 5: 验证关键字段
        print("\n[5] jobs 表关键字段验证：")
        cursor = await conn.execute("PRAGMA table_info(jobs)")
        job_cols = {row[1] for row in await cursor.fetchall()}
        required_cols = {
            "id", "title", "company", "location", "salary_range",
            "jd_text", "platform", "url", "crawled_at",
            "score", "score_reason", "match_points", "concern_points", "status"
        }
        all_ok = True
        for col in sorted(required_cols):
            ok = "[OK]" if col in job_cols else "[MISS]"
            if "[MISS]" in ok:
                all_ok = False
            print(f"    {ok:<7} jobs.{col}")

        if all_ok:
            print("    => 全部 15 个必要字段均存在")
        else:
            print("    => [FAIL] 有字段缺失！")
            return

        # Step 6: 幂等性测试
        print("\n[6] 幂等性测试（第二次调用 init_db）...")
        await init_db()
        print("    [OK] 第二次初始化无错误（CREATE TABLE IF NOT EXISTS 生效）")

    print("\n" + "=" * 60)
    print("[PASS] Phase 1 验证通过！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
