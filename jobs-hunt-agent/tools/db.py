"""
SQLite 数据库工具模块。

提供建表、读写接口，所有操作均为异步。
"""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite

from models.job_posting import JobPosting

logger = logging.getLogger(__name__)

DB_PATH = Path("data/jobs.db")

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT NOT NULL,
    salary_range    TEXT,
    jd_text         TEXT,
    platform        TEXT NOT NULL,
    url             TEXT NOT NULL,
    crawled_at      TEXT NOT NULL,
    score           REAL,
    score_reason    TEXT,
    match_points    TEXT,   -- JSON 数组
    concern_points  TEXT,   -- JSON 数组
    status          TEXT NOT NULL DEFAULT 'new'
);
"""

CREATE_APPLICATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS applications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    applied_at      TEXT NOT NULL,
    resume_path     TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / sent / failed
    screenshot_path TEXT,
    notes           TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db():
    """返回数据库连接的异步上下文管理器，自动提交/回滚。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        await conn.commit()


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """创建所有表（幂等操作）。"""
    async with get_db() as conn:
        await conn.execute(CREATE_JOBS_TABLE)
        await conn.execute(CREATE_APPLICATIONS_TABLE)
    logger.info("数据库初始化完成：%s", DB_PATH)


# ---------------------------------------------------------------------------
# jobs 表操作
# ---------------------------------------------------------------------------

async def upsert_job(job: JobPosting) -> bool:
    """
    插入或更新一条职位记录。
    返回 True 表示新插入，False 表示已存在（按 id 去重）。
    """
    async with get_db() as conn:
        existing = await conn.execute("SELECT id FROM jobs WHERE id = ?", (job.id,))
        row = await existing.fetchone()
        if row:
            return False

        await conn.execute(
            """
            INSERT INTO jobs (
                id, title, company, location, salary_range, jd_text,
                platform, url, crawled_at, score, score_reason,
                match_points, concern_points, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.title,
                job.company,
                job.location,
                job.salary_range,
                job.jd_text,
                job.platform,
                job.url,
                job.crawled_at.isoformat(),
                job.score,
                job.score_reason,
                json.dumps(job.match_points, ensure_ascii=False) if job.match_points else None,
                json.dumps(job.concern_points, ensure_ascii=False) if job.concern_points else None,
                job.status,
            ),
        )
    return True


async def update_job_score(
    job_id: str,
    score: float,
    score_reason: str,
    match_points: Optional[list[str]] = None,
    concern_points: Optional[list[str]] = None,
) -> None:
    """更新职位的筛选评分。"""
    async with get_db() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET score = ?, score_reason = ?, match_points = ?, concern_points = ?,
                status = 'filtered'
            WHERE id = ?
            """,
            (
                score,
                score_reason,
                json.dumps(match_points, ensure_ascii=False) if match_points else None,
                json.dumps(concern_points, ensure_ascii=False) if concern_points else None,
                job_id,
            ),
        )


async def update_job_status(job_id: str, status: str) -> None:
    """更新职位状态。"""
    async with get_db() as conn:
        await conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


async def get_job_by_id(job_id: str) -> Optional[JobPosting]:
    """按 ID 查询单条职位。"""
    async with get_db() as conn:
        cursor = await conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
    return _row_to_job(row) if row else None


async def get_jobs_by_status(status: str) -> list[JobPosting]:
    """按状态查询职位列表。"""
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY crawled_at DESC", (status,)
        )
        rows = await cursor.fetchall()
    return [_row_to_job(r) for r in rows]


async def get_top_jobs(n: int = 10, min_score: float = 0) -> list[JobPosting]:
    """获取评分最高的前 N 个职位。"""
    async with get_db() as conn:
        cursor = await conn.execute(
            """
            SELECT * FROM jobs
            WHERE score IS NOT NULL AND score >= ?
            ORDER BY score DESC
            LIMIT ?
            """,
            (min_score, n),
        )
        rows = await cursor.fetchall()
    return [_row_to_job(r) for r in rows]


async def job_exists(job_id: str) -> bool:
    """检查职位是否已存在。"""
    async with get_db() as conn:
        cursor = await conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
        return await cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# applications 表操作
# ---------------------------------------------------------------------------

async def insert_application(
    job_id: str,
    resume_path: str,
    status: str = "pending",
    screenshot_path: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """记录一次投递，返回自增 ID。"""
    async with get_db() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO applications (job_id, applied_at, resume_path, status, screenshot_path, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                datetime.now().isoformat(),
                resume_path,
                status,
                screenshot_path,
                notes,
            ),
        )
        return cursor.lastrowid


async def is_already_applied(job_id: str) -> bool:
    """检查某岗位是否已有投递记录（sent 状态）。"""
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM applications WHERE job_id = ? AND status = 'sent'", (job_id,)
        )
        return await cursor.fetchone() is not None


async def update_application_status(
    application_id: int,
    status: str,
    screenshot_path: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """更新投递记录状态。"""
    async with get_db() as conn:
        await conn.execute(
            """
            UPDATE applications
            SET status = ?,
                screenshot_path = COALESCE(?, screenshot_path),
                notes = COALESCE(?, notes)
            WHERE id = ?
            """,
            (status, screenshot_path, notes, application_id),
        )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _row_to_job(row: aiosqlite.Row) -> JobPosting:
    """将数据库行转换为 JobPosting 对象。"""
    data = dict(row)
    if data.get("match_points"):
        data["match_points"] = json.loads(data["match_points"])
    if data.get("concern_points"):
        data["concern_points"] = json.loads(data["concern_points"])
    return JobPosting(**data)
