"""
爬虫模块单元测试。

测试策略：
- Mock 掉 Playwright 浏览器操作（避免真实网络请求）
- Mock 掉数据库（避免测试对磁盘有副作用）
- 在爬虫类层面 mock fetch_job_list / fetch_job_detail，测试 crawl_node 编排逻辑
- 独立测试卡片解析逻辑（通过模拟 DOM 元素行为）
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.agent_state import make_initial_state
from models.job_posting import JobPosting
from tools.browser import human_delay


# ---------------------------------------------------------------------------
# Fixtures：示例数据
# ---------------------------------------------------------------------------


def make_job(
    job_id: str = "shixiseng_test001",
    title: str = "Python 实习生",
    company: str = "测试科技有限公司",
    platform: str = "shixiseng",
) -> JobPosting:
    """创建一个用于测试的 JobPosting 对象。"""
    return JobPosting(
        id=job_id,
        title=title,
        company=company,
        location="北京",
        salary_range="200元/天",
        jd_text="负责 Python 后端开发，熟悉 LangChain、FastAPI 优先。",
        platform=platform,
        url=f"https://www.shixiseng.com/intern/{job_id}",
        crawled_at=datetime(2026, 3, 27, 10, 0, 0),
    )


SAMPLE_CONFIG = {
    "search": {
        "cities": ["北京"],
        "skills_required": ["Python"],
        "skills_bonus": ["LangChain"],
        "filter_score_threshold": 65,
    },
    "platforms": {
        "shixiseng": {"enabled": True, "cookie_file": ".cookies/shixiseng.json"},
        "boss": {"enabled": False, "cookie_file": ".cookies/boss.json"},
    },
    "limits": {
        "max_jobs_per_run": 5,
        "request_delay_seconds": [0.01, 0.02],  # 测试中极短延迟
    },
}


# ---------------------------------------------------------------------------
# 测试：human_delay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_delay_calls_sleep():
    """human_delay 应调用 asyncio.sleep，延迟在合理范围内。"""
    sleep_calls = []

    async def fake_sleep(sec):
        sleep_calls.append(sec)

    with patch("tools.browser.asyncio.sleep", side_effect=fake_sleep):
        await human_delay(1.0, 2.0)

    assert len(sleep_calls) == 1
    assert 1.0 <= sleep_calls[0] <= 2.0


@pytest.mark.asyncio
async def test_human_delay_randomness():
    """多次调用 human_delay 应产生不同延迟（概率性，极少情况可能相同）。"""
    results = []
    with patch("tools.browser.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        for _ in range(10):
            await human_delay(0.0, 1.0)
            results.append(mock_sleep.call_args[0][0])

    # 10 次结果不全相同（如果全相同说明随机性有问题）
    assert len(set(round(r, 6) for r in results)) > 1


# ---------------------------------------------------------------------------
# 测试：ShixisengCrawler._parse_card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shixiseng_parse_card_success():
    """_parse_card 正常解析时应返回完整 JobPosting（jd_text 为空）。"""
    from agents.crawler import ShixisengCrawler

    # Mock BrowserManager 和 config
    bm = MagicMock()
    crawler = ShixisengCrawler(bm, SAMPLE_CONFIG)

    # 构造 Mock 的 DOM 元素
    mock_card = AsyncMock()

    async def mock_query_selector(selector):
        el = AsyncMock()
        if "a.position" in selector or "a[href" in selector:
            el.get_attribute = AsyncMock(return_value="/intern/test001")
            return el
        return None

    mock_card.query_selector = mock_query_selector

    # Mock safe_get_text 返回值
    with patch("agents.crawler.safe_get_text") as mock_get_text:
        mock_get_text.side_effect = lambda el, sel: {
            ".position": asyncio.coroutine(lambda: "Python AI 实习生")(),
            ".title": asyncio.coroutine(lambda: None)(),
            ".intern-name": asyncio.coroutine(lambda: None)(),
            ".company-name": asyncio.coroutine(lambda: "测试公司")(),
            ".company": asyncio.coroutine(lambda: None)(),
            ".firm-name": asyncio.coroutine(lambda: None)(),
            ".city": asyncio.coroutine(lambda: "北京")(),
            ".address": asyncio.coroutine(lambda: None)(),
            ".work-place": asyncio.coroutine(lambda: None)(),
            ".day-money": asyncio.coroutine(lambda: "200元/天")(),
            ".salary": asyncio.coroutine(lambda: None)(),
            ".money": asyncio.coroutine(lambda: None)(),
        }.get(sel, asyncio.coroutine(lambda: None)())

        result = await crawler._parse_card(mock_card)

    assert result is not None
    assert result.id == "shixiseng_test001"
    assert result.platform == "shixiseng"
    assert result.jd_text == ""  # 列表页不抓详情


@pytest.mark.asyncio
async def test_shixiseng_parse_card_missing_link():
    """卡片没有有效链接时，_parse_card 应返回 None（不抛异常）。"""
    from agents.crawler import ShixisengCrawler

    bm = MagicMock()
    crawler = ShixisengCrawler(bm, SAMPLE_CONFIG)

    mock_card = AsyncMock()
    mock_card.query_selector = AsyncMock(return_value=None)  # 所有选择器返回 None

    result = await crawler._parse_card(mock_card)
    assert result is None


@pytest.mark.asyncio
async def test_shixiseng_parse_card_missing_title_company():
    """标题或公司为空时，_parse_card 应返回 None。"""
    from agents.crawler import ShixisengCrawler

    bm = MagicMock()
    crawler = ShixisengCrawler(bm, SAMPLE_CONFIG)

    mock_card = AsyncMock()

    async def mock_query_selector(selector):
        el = AsyncMock()
        el.get_attribute = AsyncMock(return_value="/intern/test002")
        return el

    mock_card.query_selector = mock_query_selector

    with patch("agents.crawler.safe_get_text", return_value=None):
        result = await crawler._parse_card(mock_card)

    assert result is None


# ---------------------------------------------------------------------------
# 测试：BossCrawler._parse_card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boss_parse_card_success():
    """Boss直聘 _parse_card 应正确提取 job_id（从 URL 路径提取）。"""
    from agents.crawler import BossCrawler

    bm = MagicMock()
    crawler = BossCrawler(bm, SAMPLE_CONFIG)

    mock_card = AsyncMock()

    async def mock_query_selector(selector):
        el = AsyncMock()
        if "a.job-card-left" in selector or "a[href*='/job_detail/']" in selector:
            el.get_attribute = AsyncMock(
                return_value="/job_detail/abc123xyz.html?lid=xxx"
            )
            return el
        return None

    mock_card.query_selector = mock_query_selector

    with patch("agents.crawler.safe_get_text") as mock_get_text:
        side_effects = {
            ".job-name": "Python 后端开发",
            ".job-title": None,
            ".salary": "20-30K",
            ".company-name": "某某科技",
            ".company-text": None,
            ".job-area": "上海",
            ".job-area-wrapper": None,
        }

        async def side_effect(el, sel):
            return side_effects.get(sel)

        mock_get_text.side_effect = side_effect

        result = await crawler._parse_card(mock_card)

    assert result is not None
    assert result.id == "boss_abc123xyz"  # .html 应被去除
    assert result.platform == "boss"
    assert result.salary_range == "20-30K"


# ---------------------------------------------------------------------------
# 测试：crawl_node 编排逻辑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crawl_node_shixiseng_only():
    """crawl_node 应正确调用实习僧爬虫并将新职位写入 state.jobs_found。"""
    from agents.crawler import crawl_node

    sample_job = make_job()
    sample_job_no_jd = sample_job.model_copy(update={"jd_text": ""})

    initial_state = make_initial_state(SAMPLE_CONFIG, run_mode="dry-run")

    with (
        patch("agents.crawler.init_db", new_callable=AsyncMock),
        patch("agents.crawler.job_exists", new_callable=AsyncMock, return_value=False),
        patch("agents.crawler.upsert_job", new_callable=AsyncMock, return_value=True),
        patch("agents.crawler.BrowserManager") as mock_bm_cls,
        patch("agents.crawler.ShixisengCrawler") as mock_crawler_cls,
    ):
        # 配置 BrowserManager 作为异步上下文管理器
        mock_bm_instance = AsyncMock()
        mock_bm_cls.return_value.__aenter__ = AsyncMock(return_value=mock_bm_instance)
        mock_bm_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # 配置 ShixisengCrawler
        mock_crawler = AsyncMock()
        mock_crawler.fetch_job_list = AsyncMock(
            side_effect=[[sample_job_no_jd], []]  # 第一页有结果，第二页为空
        )
        mock_crawler.fetch_job_detail = AsyncMock(return_value=sample_job.jd_text)
        mock_crawler_cls.return_value = mock_crawler

        result_state = await crawl_node(initial_state)

    assert result_state["current_phase"] == "filter"
    assert len(result_state["jobs_found"]) == 1
    found_job = result_state["jobs_found"][0]
    assert found_job.id == sample_job.id
    assert found_job.jd_text == sample_job.jd_text  # 详情已补充


@pytest.mark.asyncio
async def test_crawl_node_deduplication():
    """crawl_node 应跳过数据库中已存在的职位（job_exists 返回 True）。"""
    from agents.crawler import crawl_node

    sample_job_no_jd = make_job().model_copy(update={"jd_text": ""})
    initial_state = make_initial_state(SAMPLE_CONFIG, run_mode="dry-run")

    with (
        patch("agents.crawler.init_db", new_callable=AsyncMock),
        patch("agents.crawler.job_exists", new_callable=AsyncMock, return_value=True),  # 已存在
        patch("agents.crawler.upsert_job", new_callable=AsyncMock) as mock_upsert,
        patch("agents.crawler.BrowserManager") as mock_bm_cls,
        patch("agents.crawler.ShixisengCrawler") as mock_crawler_cls,
    ):
        mock_bm_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_bm_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_crawler = AsyncMock()
        mock_crawler.fetch_job_list = AsyncMock(side_effect=[[sample_job_no_jd], []])
        mock_crawler.fetch_job_detail = AsyncMock(return_value="JD 文本")
        mock_crawler_cls.return_value = mock_crawler

        result_state = await crawl_node(initial_state)

    # 已存在 → upsert 不应被调用，jobs_found 应为空
    mock_upsert.assert_not_called()
    assert len(result_state["jobs_found"]) == 0


@pytest.mark.asyncio
async def test_crawl_node_empty_jd_skips_job():
    """fetch_job_detail 返回空字符串时，该职位应被跳过并记录到 errors。"""
    from agents.crawler import crawl_node

    sample_job_no_jd = make_job().model_copy(update={"jd_text": ""})
    initial_state = make_initial_state(SAMPLE_CONFIG, run_mode="dry-run")

    with (
        patch("agents.crawler.init_db", new_callable=AsyncMock),
        patch("agents.crawler.job_exists", new_callable=AsyncMock, return_value=False),
        patch("agents.crawler.upsert_job", new_callable=AsyncMock),
        patch("agents.crawler.BrowserManager") as mock_bm_cls,
        patch("agents.crawler.ShixisengCrawler") as mock_crawler_cls,
    ):
        mock_bm_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_bm_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_crawler = AsyncMock()
        mock_crawler.fetch_job_list = AsyncMock(side_effect=[[sample_job_no_jd], []])
        mock_crawler.fetch_job_detail = AsyncMock(return_value="")  # 空 JD
        mock_crawler_cls.return_value = mock_crawler

        result_state = await crawl_node(initial_state)

    assert len(result_state["jobs_found"]) == 0
    assert len(result_state["errors"]) == 1
    assert "JD 文本为空" in result_state["errors"][0]["error"]


@pytest.mark.asyncio
async def test_crawl_node_boss_skipped_when_not_logged_in():
    """Boss直聘 登录检查失败时应跳过该平台，并将错误记录到 state.errors。"""
    from agents.crawler import crawl_node

    config_with_boss = {
        **SAMPLE_CONFIG,
        "platforms": {
            "shixiseng": {"enabled": False, "cookie_file": ".cookies/shixiseng.json"},
            "boss": {"enabled": True, "cookie_file": ".cookies/boss.json"},
        },
    }
    initial_state = make_initial_state(config_with_boss, run_mode="dry-run")

    with (
        patch("agents.crawler.init_db", new_callable=AsyncMock),
        patch("agents.crawler.BrowserManager") as mock_bm_cls,
        patch("agents.crawler.BossCrawler") as mock_crawler_cls,
    ):
        mock_bm_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_bm_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_crawler = AsyncMock(spec=["ensure_logged_in", "fetch_job_list", "fetch_job_detail"])
        mock_crawler.ensure_logged_in = AsyncMock(return_value=False)  # 未登录
        mock_crawler.fetch_job_list = AsyncMock()
        mock_crawler_cls.return_value = mock_crawler

        result_state = await crawl_node(initial_state)

    # fetch_job_list 不应被调用
    mock_crawler.fetch_job_list.assert_not_called()
    # 应有错误记录
    assert any("登录" in e["error"] for e in result_state["errors"])


@pytest.mark.asyncio
async def test_crawl_node_respects_max_jobs_limit():
    """crawl_node 应在达到 max_jobs_per_run 上限后停止抓取。"""
    from agents.crawler import crawl_node

    config_small_limit = {
        **SAMPLE_CONFIG,
        "limits": {"max_jobs_per_run": 2, "request_delay_seconds": [0.01, 0.02]},
    }
    initial_state = make_initial_state(config_small_limit, run_mode="dry-run")

    # 准备 3 个不重复的职位
    jobs_page1 = [
        make_job(f"shixiseng_j{i}").model_copy(update={"jd_text": ""})
        for i in range(3)
    ]

    with (
        patch("agents.crawler.init_db", new_callable=AsyncMock),
        patch("agents.crawler.job_exists", new_callable=AsyncMock, return_value=False),
        patch("agents.crawler.upsert_job", new_callable=AsyncMock, return_value=True),
        patch("agents.crawler.BrowserManager") as mock_bm_cls,
        patch("agents.crawler.ShixisengCrawler") as mock_crawler_cls,
    ):
        mock_bm_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_bm_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        mock_crawler = AsyncMock()
        mock_crawler.fetch_job_list = AsyncMock(side_effect=[jobs_page1, []])
        mock_crawler.fetch_job_detail = AsyncMock(return_value="JD 文本")
        mock_crawler_cls.return_value = mock_crawler

        result_state = await crawl_node(initial_state)

    # 只应收集 2 个（max_jobs_per_run=2）
    assert len(result_state["jobs_found"]) == 2


@pytest.mark.asyncio
async def test_crawl_node_platform_exception_handled():
    """平台整体异常时，crawl_node 应记录错误但不崩溃，current_phase 仍为 filter。"""
    from agents.crawler import crawl_node

    initial_state = make_initial_state(SAMPLE_CONFIG, run_mode="dry-run")

    with (
        patch("agents.crawler.init_db", new_callable=AsyncMock),
        patch("agents.crawler._run_platform_crawl", new_callable=AsyncMock) as mock_run,
    ):
        mock_run.side_effect = RuntimeError("模拟网络异常")

        result_state = await crawl_node(initial_state)

    assert result_state["current_phase"] == "filter"
    assert len(result_state["errors"]) >= 1
    assert "模拟网络异常" in result_state["errors"][0]["error"]


# ---------------------------------------------------------------------------
# 测试：BOSS_CITY_CODES 映射
# ---------------------------------------------------------------------------


def test_boss_city_codes_coverage():
    """BOSS_CITY_CODES 应包含常用城市，且编码格式正确（纯数字，12 位）。"""
    from agents.crawler import BOSS_CITY_CODES

    required_cities = ["北京", "上海", "深圳", "广州", "全国"]
    for city in required_cities:
        assert city in BOSS_CITY_CODES, f"缺少城市：{city}"
        code = BOSS_CITY_CODES[city]
        assert code.isdigit(), f"城市编码应为纯数字：{city} → {code}"
        assert len(code) == 9, f"城市编码长度应为 9 位：{city} → {code}"
