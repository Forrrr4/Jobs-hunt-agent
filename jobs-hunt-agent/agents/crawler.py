"""
岗位信息采集模块。

支持平台：
  - 实习僧 (shixiseng.com)：无需登录，公开访问
  - Boss直聘 (zhipin.com)：需要 cookie 登录态

每个爬虫职责：
  1. 从列表页抓取职位基本信息（标题、公司、薪资、URL）
  2. 进入详情页抓取完整 JD 文本
  3. 返回标准化的 JobPosting 对象列表

crawl_node() 是 LangGraph 节点，负责：
  - 调用各平台爬虫
  - 去重（跳过数据库中已有记录）
  - 写入数据库
  - 更新 AgentState
"""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from models.agent_state import AgentState
from models.job_posting import JobPosting
from tools.browser import BrowserManager, human_delay, safe_get_attr, safe_get_text
from tools.db import init_db, job_exists, upsert_job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Boss直聘城市编码映射（cityCode 参数）
# ---------------------------------------------------------------------------

BOSS_CITY_CODES: dict[str, str] = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "武汉": "101200100",
    "西安": "101110100",
    "南京": "101190100",
    "苏州": "101190400",
    "厦门": "101230200",
    "重庆": "101040100",
    "长沙": "101250100",
    "青岛": "101120200",
    "全国": "100010000",
}


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


class BaseCrawler(ABC):
    """爬虫抽象基类，定义统一接口。"""

    PLATFORM: str = ""

    def __init__(self, browser_manager: BrowserManager, config: dict):
        self.bm = browser_manager
        self.config = config
        limits = config.get("limits", {})
        delay = limits.get("request_delay_seconds", [2.0, 5.0])
        self._delay_min: float = float(delay[0])
        self._delay_max: float = float(delay[1])

    async def _delay(self) -> None:
        await human_delay(self._delay_min, self._delay_max)

    @abstractmethod
    async def fetch_job_list(
        self, keywords: list[str], city: str, page: int = 1
    ) -> list[JobPosting]:
        """抓取职位列表页，返回含基本信息（jd_text 为空）的 JobPosting 列表。"""

    @abstractmethod
    async def fetch_job_detail(self, job_id: str, url: str) -> str:
        """抓取职位详情页，返回完整 JD 文本。"""


# ---------------------------------------------------------------------------
# 实习僧爬虫
# ---------------------------------------------------------------------------


class ShixisengCrawler(BaseCrawler):
    """
    实习僧 (shixiseng.com) 爬虫。

    无需登录，通过 Playwright 解析页面 DOM 抓取职位信息。
    搜索 URL：https://www.shixiseng.com/interns?keyword={keyword}&city={city}&page={page}
    """

    PLATFORM = "shixiseng"
    BASE_URL = "https://www.shixiseng.com"
    SEARCH_URL = "https://www.shixiseng.com/interns"

    async def fetch_job_list(
        self, keywords: list[str], city: str, page: int = 1
    ) -> list[JobPosting]:
        """抓取实习僧职位列表，返回 JobPosting 列表（jd_text 为空，待详情页补充）。"""
        keyword_str = quote(" ".join(keywords))
        url = f"{self.SEARCH_URL}?keyword={keyword_str}&city={city}&page={page}"

        page_obj = await self.bm.new_page()
        try:
            logger.info("[实习僧] 列表页 page=%d | %s", page, url)
            # 使用 load 而非 networkidle：shixiseng 的 networkidle 经常超时
            success = await self.bm.goto_with_retry(page_obj, url, wait_until="load")
            if not success:
                return []

            await self._delay()

            # 等待职位卡片出现
            try:
                await page_obj.wait_for_selector(
                    ".intern-item, .f-intern-item", timeout=15_000
                )
            except Exception:
                logger.warning("[实习僧] 未找到职位卡片，可能触发反爬或无结果")
                return []

            cards = await page_obj.query_selector_all(".intern-item, .f-intern-item")
            logger.info("[实习僧] 找到 %d 个职位卡片", len(cards))

            jobs: list[JobPosting] = []
            for card in cards:
                job = await self._parse_card(card)
                if job:
                    jobs.append(job)

            return jobs

        except Exception as e:
            logger.error("[实习僧] 列表抓取异常 page=%d: %s", page, e)
            return []
        finally:
            await page_obj.close()

    async def _parse_card(self, card) -> Optional[JobPosting]:
        """
        从列表页卡片元素解析 JobPosting（无 jd_text）。

        实习僧实际 DOM 结构（2024-2025）：
          .intern-detail
            .intern-detail__job
              a.title          ← 职位名称 & href（详情链接）
              span.day         ← 薪资（如 "150/天"）
              span.city        ← 城市
            .intern-detail__company
              a.title          ← 公司名称（注意同类名，需按父容器区分）
        """
        try:
            # 提取详情链接：.intern-detail__job 内的 a.title
            job_section = await card.query_selector(".intern-detail__job")
            link_el = None
            if job_section:
                link_el = await job_section.query_selector("a.title, a[href*='/intern/']")
            if not link_el:
                # 兜底：卡片内任何包含 /intern/ 的链接
                link_el = await card.query_selector("a[href*='/intern/']")
            if not link_el:
                link_el = await card.query_selector("a")

            href = await link_el.get_attribute("href") if link_el else None
            if not href:
                return None

            full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
            # 从路径提取 job_id：/intern/inn_xxxxx?pcm=... → inn_xxxxx
            raw_id = href.split("/intern/")[-1].split("?")[0].rstrip("/")
            job_id = f"shixiseng_{raw_id}"

            # 职位名称（.intern-detail__job 内的 a.title 文本）
            title = None
            if job_section:
                title = await safe_get_text(job_section, "a.title")
            if not title:
                title = await safe_get_text(card, ".intern-detail__job a.title")

            # 公司名称（.intern-detail__company 内的 a.title 文本）
            company_section = await card.query_selector(".intern-detail__company")
            company = None
            if company_section:
                company = await safe_get_text(company_section, "a.title")
            if not company:
                company = await safe_get_text(card, ".intern-detail__company a.title")

            # 城市（span.city）
            location = await safe_get_text(card, "span.city") or await safe_get_text(card, ".city")

            # 薪资（span.day，如 "150/天"；"-/天" 表示面议）
            salary_raw = await safe_get_text(card, "span.day") or await safe_get_text(card, ".day")
            # 过滤无意义的占位值
            salary = None
            if salary_raw and salary_raw not in ("-/天", "0/天", "/天"):
                salary = salary_raw

            if not title or not company:
                return None

            return JobPosting(
                id=job_id,
                title=title.strip(),
                company=company.strip(),
                location=(location or "").strip(),
                salary_range=salary.strip() if salary else None,
                jd_text="",  # 详情页补充
                platform=self.PLATFORM,
                url=full_url,
            )

        except Exception as e:
            logger.debug("[实习僧] 解析卡片失败：%s", e)
            return None

    async def fetch_job_detail(self, job_id: str, url: str) -> str:
        """抓取实习僧职位详情页，返回 JD 全文。"""
        page_obj = await self.bm.new_page()
        try:
            logger.debug("[实习僧] 详情页：%s", url)
            success = await self.bm.goto_with_retry(page_obj, url, wait_until="load")
            if not success:
                return ""

            await self._delay()

            # 按优先级尝试 JD 文本容器选择器
            # 实习僧实际 class（2024-2025 版）：.job-content > .content_left > .job_detail
            for selector in [
                ".job-content",        # 最佳：含"职位描述："+ 完整 JD
                ".content_left",       # 次选：略去右侧公司简介
                ".job_detail",         # 备用：纯 JD 文本
                ".intern-detail-page", # 兜底：整个详情区域
                ".content",
                ".intern-content",
            ]:
                text = await safe_get_text(page_obj, selector)
                if text and len(text) > 50:
                    return text

            # 兜底：提取整个 body 文本
            logger.warning("[实习僧] 未找到 JD 容器，使用 body 文本：%s", url)
            return (await page_obj.inner_text("body")).strip()

        except Exception as e:
            logger.error("[实习僧] 详情抓取失败 %s: %s", url, e)
            return ""
        finally:
            await page_obj.close()


# ---------------------------------------------------------------------------
# Boss直聘爬虫
# ---------------------------------------------------------------------------


class BossCrawler(BaseCrawler):
    """
    Boss直聘 (zhipin.com) 爬虫。

    需要登录态 cookie（存于 .cookies/boss.json）。
    搜索 URL：https://www.zhipin.com/web/geek/job?query={query}&city={city_code}&page={page}
    """

    PLATFORM = "boss"
    BASE_URL = "https://www.zhipin.com"
    SEARCH_URL = "https://www.zhipin.com/web/geek/job"
    # 登录后才会出现的元素（个人头像/昵称区域）
    LOGIN_INDICATOR = ".nav-figure, .user-nav"
    LOGIN_PAGE_URL = "https://www.zhipin.com/web/user/?ka=header-login"

    async def ensure_logged_in(self) -> bool:
        """
        检查 cookie 是否有效（是否处于登录状态）。

        Returns:
            True 表示已登录，False 表示 cookie 失效或未配置
        """
        page_obj = await self.bm.new_page()
        try:
            success = await self.bm.goto_with_retry(
                page_obj, self.BASE_URL, wait_until="domcontentloaded"
            )
            if not success:
                return False

            logged_in = await self.bm.check_login_status(page_obj, self.LOGIN_INDICATOR)
            if logged_in:
                logger.info("[Boss直聘] Cookie 有效，已登录")
            else:
                logger.warning(
                    "[Boss直聘] Cookie 失效或未登录。请手动登录后将 cookie 导出到 %s",
                    self.config.get("platforms", {})
                    .get("boss", {})
                    .get("cookie_file", ".cookies/boss.json"),
                )
            return logged_in

        except Exception as e:
            logger.error("[Boss直聘] 登录状态检测失败：%s", e)
            return False
        finally:
            await page_obj.close()

    async def fetch_job_list(
        self, keywords: list[str], city: str, page: int = 1
    ) -> list[JobPosting]:
        """抓取 Boss直聘 职位列表（需登录）。"""
        city_code = BOSS_CITY_CODES.get(city, BOSS_CITY_CODES["全国"])
        query = quote(" ".join(keywords))
        url = f"{self.SEARCH_URL}?query={query}&city={city_code}&page={page}"

        page_obj = await self.bm.new_page()
        try:
            logger.info("[Boss直聘] 列表页 page=%d | %s", page, url)
            success = await self.bm.goto_with_retry(page_obj, url, wait_until="networkidle")
            if not success:
                return []

            await self._delay()

            # 检测是否被要求登录或触发验证码
            if await self._is_blocked(page_obj):
                logger.warning("[Boss直聘] 检测到登录拦截或验证码，跳过当前页")
                return []

            try:
                await page_obj.wait_for_selector(".job-card-wrapper", timeout=15_000)
            except Exception:
                logger.warning("[Boss直聘] 未找到职位卡片，可能无结果或触发反爬")
                return []

            cards = await page_obj.query_selector_all(".job-card-wrapper")
            logger.info("[Boss直聘] 找到 %d 个职位卡片", len(cards))

            jobs: list[JobPosting] = []
            for card in cards:
                job = await self._parse_card(card)
                if job:
                    jobs.append(job)

            return jobs

        except Exception as e:
            logger.error("[Boss直聘] 列表抓取异常 page=%d: %s", page, e)
            return []
        finally:
            await page_obj.close()

    async def _parse_card(self, card) -> Optional[JobPosting]:
        """从 Boss直聘 列表页卡片解析 JobPosting（无 jd_text）。"""
        try:
            # 提取详情链接
            link_el = await card.query_selector("a.job-card-left, a[href*='/job_detail/']")
            if not link_el:
                link_el = await card.query_selector("a")
            href = await link_el.get_attribute("href") if link_el else None
            if not href:
                return None

            full_url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
            # Boss 详情 URL 形如 /job_detail/xxxxx.html
            raw_id = re.search(r"/job_detail/([^/?#]+)", href)
            job_id_str = raw_id.group(1).replace(".html", "") if raw_id else href.split("/")[-1]
            job_id = f"boss_{job_id_str}"

            title = (
                await safe_get_text(card, ".job-name")
                or await safe_get_text(card, ".job-title")
            )
            salary = await safe_get_text(card, ".salary")
            company = (
                await safe_get_text(card, ".company-name")
                or await safe_get_text(card, ".company-text")
            )
            location = (
                await safe_get_text(card, ".job-area")
                or await safe_get_text(card, ".job-area-wrapper")
            )

            if not title or not company:
                return None

            return JobPosting(
                id=job_id,
                title=title.strip(),
                company=company.strip(),
                location=(location or "").strip(),
                salary_range=salary.strip() if salary else None,
                jd_text="",
                platform=self.PLATFORM,
                url=full_url,
            )

        except Exception as e:
            logger.debug("[Boss直聘] 解析卡片失败：%s", e)
            return None

    async def fetch_job_detail(self, job_id: str, url: str) -> str:
        """抓取 Boss直聘 职位详情页，返回 JD 全文。"""
        page_obj = await self.bm.new_page()
        try:
            logger.debug("[Boss直聘] 详情页：%s", url)
            success = await self.bm.goto_with_retry(page_obj, url, wait_until="networkidle")
            if not success:
                return ""

            await self._delay()

            if await self._is_blocked(page_obj):
                logger.warning("[Boss直聘] 详情页被拦截：%s", url)
                return ""

            for selector in [
                ".job-detail-section",
                ".job-sec-text",
                ".desc",
                ".job-detail",
                ".text",
            ]:
                text = await safe_get_text(page_obj, selector)
                if text and len(text) > 50:
                    return text

            logger.warning("[Boss直聘] 未找到 JD 容器，使用 body 文本：%s", url)
            return (await page_obj.inner_text("body")).strip()

        except Exception as e:
            logger.error("[Boss直聘] 详情抓取失败 %s: %s", url, e)
            return ""
        finally:
            await page_obj.close()

    async def _is_blocked(self, page) -> bool:
        """检测是否被重定向到登录/验证码页面。"""
        try:
            current_url = page.url
            # 登录页面或验证码页面特征
            blocked_patterns = ["/user/?", "/captcha", "login", "verify"]
            if any(p in current_url for p in blocked_patterns):
                return True
            # 检测验证码弹窗
            captcha_el = await page.query_selector(".captcha-dialog, #captcha, .verify-wrap")
            return captcha_el is not None
        except Exception:
            return False


# ---------------------------------------------------------------------------
# LangGraph 节点
# ---------------------------------------------------------------------------


async def crawl_node(state: AgentState) -> AgentState:
    """
    LangGraph 节点：执行全平台岗位抓取。

    流程：
    1. 读取 config 中的平台、城市、关键词配置
    2. 为每个启用的平台创建 BrowserManager 和爬虫实例
    3. 按关键词 × 城市 × 页码遍历抓取列表
    4. 对每个职位：去重 → 抓取详情 → 写入数据库
    5. 更新 AgentState.jobs_found 和 current_phase

    Args:
        state: 当前 AgentState（含 config、run_mode 等字段）

    Returns:
        更新后的 AgentState
    """
    await init_db()

    config = state["config"]
    search_cfg = config.get("search", {})
    platforms_cfg = config.get("platforms", {})
    limits_cfg = config.get("limits", {})

    keywords: list[str] = search_cfg.get("skills_required", []) + search_cfg.get(
        "skills_bonus", []
    )
    if not keywords:
        keywords = ["Python", "AI"]

    cities: list[str] = search_cfg.get("cities", ["全国"])
    max_jobs: int = limits_cfg.get("max_jobs_per_run", 50)

    all_jobs: list[JobPosting] = list(state.get("jobs_found", []))
    errors: list[dict] = list(state.get("errors", []))

    logger.info(
        "开始抓取 | 关键词=%s | 城市=%s | 上限=%d", keywords, cities, max_jobs
    )

    # ----------------------------------------------------------------
    # 实习僧
    # ----------------------------------------------------------------
    shixiseng_cfg = platforms_cfg.get("shixiseng", {})
    if shixiseng_cfg.get("enabled", False):
        cookie_file = shixiseng_cfg.get("cookie_file")
        try:
            all_jobs, errors = await _run_platform_crawl(
                platform_name="shixiseng",
                crawler_cls=ShixisengCrawler,
                cookie_file=cookie_file,
                config=config,
                keywords=keywords,
                cities=cities,
                max_jobs=max_jobs,
                existing_jobs=all_jobs,
                errors=errors,
                check_login=False,
            )
        except Exception as e:
            logger.error("[实习僧] 平台整体异常：%s", e)
            errors.append({"module": "crawler.shixiseng", "error": str(e), "job_id": None})

    # ----------------------------------------------------------------
    # Boss直聘
    # ----------------------------------------------------------------
    boss_cfg = platforms_cfg.get("boss", {})
    if boss_cfg.get("enabled", False):
        cookie_file = boss_cfg.get("cookie_file")
        try:
            all_jobs, errors = await _run_platform_crawl(
                platform_name="boss",
                crawler_cls=BossCrawler,
                cookie_file=cookie_file,
                config=config,
                keywords=keywords,
                cities=cities,
                max_jobs=max_jobs,
                existing_jobs=all_jobs,
                errors=errors,
                check_login=True,
            )
        except Exception as e:
            logger.error("[Boss直聘] 平台整体异常：%s", e)
            errors.append({"module": "crawler.boss", "error": str(e), "job_id": None})

    logger.info("抓取完成 | 新增职位：%d 个", len(all_jobs))

    return {
        **state,
        "jobs_found": all_jobs,
        "errors": errors,
        "current_phase": "filter",
    }


# ---------------------------------------------------------------------------
# 内部辅助：单平台抓取流程
# ---------------------------------------------------------------------------


async def _run_platform_crawl(
    platform_name: str,
    crawler_cls: type[BaseCrawler],
    cookie_file: Optional[str],
    config: dict,
    keywords: list[str],
    cities: list[str],
    max_jobs: int,
    existing_jobs: list[JobPosting],
    errors: list[dict],
    check_login: bool,
) -> tuple[list[JobPosting], list[dict]]:
    """
    执行单个平台的完整抓取流程。

    Returns:
        (更新后的 all_jobs, 更新后的 errors)
    """
    # 统计已收集数量（含之前平台的结果）
    collected = len(existing_jobs)
    new_jobs: list[JobPosting] = []

    async with BrowserManager(cookie_file=cookie_file) as bm:
        crawler: BaseCrawler = crawler_cls(bm, config)

        # 登录检查（仅 Boss直聘 需要）
        if check_login and isinstance(crawler, BossCrawler):
            logged_in = await crawler.ensure_logged_in()
            if not logged_in:
                errors.append({
                    "module": f"crawler.{platform_name}",
                    "error": "Cookie 失效或未登录，跳过该平台",
                    "job_id": None,
                })
                logger.warning("[%s] 跳过：未登录", platform_name)
                return existing_jobs, errors

        # 按城市和页码遍历
        for city in cities:
            if collected >= max_jobs:
                logger.info("[%s] 已达上限 %d，停止抓取", platform_name, max_jobs)
                break

            page_num = 1
            while collected < max_jobs:
                jobs_on_page = await crawler.fetch_job_list(keywords, city, page=page_num)

                if not jobs_on_page:
                    logger.info("[%s] 城市=%s page=%d 无更多结果", platform_name, city, page_num)
                    break

                for job in jobs_on_page:
                    if collected >= max_jobs:
                        break

                    # 去重：数据库中已存在则跳过
                    if await job_exists(job.id):
                        logger.debug("[%s] 已存在，跳过：%s", platform_name, job.id)
                        continue

                    # 抓取详情页补充 JD 文本
                    jd_text = await crawler.fetch_job_detail(job.id, job.url)
                    if not jd_text:
                        logger.warning("[%s] JD 为空，跳过：%s", platform_name, job.url)
                        errors.append({
                            "module": f"crawler.{platform_name}",
                            "error": "JD 文本为空",
                            "job_id": job.id,
                        })
                        continue

                    # 补充 JD 并入库
                    job = job.model_copy(update={"jd_text": jd_text})
                    inserted = await upsert_job(job)
                    if inserted:
                        new_jobs.append(job)
                        collected += 1
                        logger.info(
                            "[%s] 新增 (%d/%d)：%s @ %s",
                            platform_name, collected, max_jobs, job.title, job.company,
                        )

                page_num += 1

    return existing_jobs + new_jobs, errors
