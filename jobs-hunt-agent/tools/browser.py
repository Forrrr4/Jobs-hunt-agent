"""
Playwright 浏览器工具封装。

提供：
- BrowserManager：异步上下文管理器，管理浏览器生命周期
- human_delay()：随机延迟，模拟人类操作节奏
- Cookie 持久化：自动加载/保存 cookie
- 反检测初始化脚本：规避常见的 WebDriver 检测
"""

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 反检测初始化脚本（注入每个页面）
# ---------------------------------------------------------------------------

_STEALTH_SCRIPT = """
// 隐藏 WebDriver 标志
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 模拟真实的 Chrome 运行时
window.chrome = {runtime: {}};

// 模拟语言设置
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});

// 模拟插件（真实浏览器有多个插件）
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        return {length: 3, 0: {name: 'Chrome PDF Plugin'}, 1: {name: 'Chrome PDF Viewer'}, 2: {name: 'Native Client'}};
    }
});

// 修正 permissions.query（无头模式返回值异常）
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({state: Notification.permission})
        : originalQuery(parameters)
);
"""

# 常见桌面 Chrome User-Agent
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.130 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 公共工具函数
# ---------------------------------------------------------------------------


async def human_delay(min_sec: float = 2.0, max_sec: float = 5.0) -> None:
    """
    模拟人类操作的随机延迟。

    在每次页面操作（点击、跳转、翻页）前调用，避免触发频率检测。
    延迟范围从 config.yaml 的 limits.request_delay_seconds 读取。
    """
    delay = random.uniform(min_sec, max_sec)
    logger.debug("等待 %.2f 秒...", delay)
    await asyncio.sleep(delay)


async def safe_get_text(page_or_element, selector: str) -> Optional[str]:
    """
    安全地从页面或元素中提取文本，找不到时返回 None 而非抛出异常。
    """
    try:
        el = await page_or_element.query_selector(selector)
        if el:
            return (await el.inner_text()).strip() or None
    except Exception:
        pass
    return None


async def safe_get_attr(page_or_element, selector: str, attr: str) -> Optional[str]:
    """安全地提取属性值。"""
    try:
        el = await page_or_element.query_selector(selector)
        if el:
            val = await el.get_attribute(attr)
            return val.strip() if val else None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# BrowserManager
# ---------------------------------------------------------------------------


class BrowserManager:
    """
    Playwright 浏览器生命周期管理器。

    用法（推荐）：
        async with BrowserManager(cookie_file=".cookies/boss.json") as bm:
            page = await bm.new_page()
            await page.goto("https://example.com")

    或手动管理：
        bm = BrowserManager()
        await bm.start()
        page = await bm.new_page()
        await bm.stop()
    """

    def __init__(
        self,
        headless: bool = True,
        cookie_file: Optional[str] = None,
        user_agent: str = _DEFAULT_USER_AGENT,
        slow_mo: int = 0,
    ):
        """
        Args:
            headless:    是否使用无头模式（生产用 True，调试用 False）
            cookie_file: cookie 持久化路径，为 None 则不持久化
            user_agent:  自定义 User-Agent
            slow_mo:     每个操作的额外延迟（ms），调试用
        """
        self.headless = headless
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.user_agent = user_agent
        self.slow_mo = slow_mo

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 Playwright、浏览器和浏览器上下文。"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
        )

        context_options: dict = {
            "user_agent": self.user_agent,
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "extra_http_headers": {
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        }

        self._context = await self._browser.new_context(**context_options)

        # 加载已有 cookie
        if self.cookie_file and self.cookie_file.exists():
            await self._load_cookies_from_file()

        # 注入反检测脚本（对所有新页面生效）
        await self._context.add_init_script(_STEALTH_SCRIPT)

        logger.info(
            "浏览器已启动 [headless=%s, cookie=%s]",
            self.headless,
            self.cookie_file or "无",
        )

    async def stop(self) -> None:
        """保存 cookie，关闭浏览器和 Playwright。"""
        try:
            if self._context:
                await self.save_cookies()
                await self._context.close()
        except Exception as e:
            logger.warning("关闭浏览器上下文时出错：%s", e)
        finally:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
            logger.info("浏览器已关闭")

    # ------------------------------------------------------------------
    # 页面操作
    # ------------------------------------------------------------------

    async def new_page(self) -> Page:
        """创建并返回一个新页面（Tab）。"""
        if not self._context:
            raise RuntimeError("BrowserManager 未启动，请先调用 start() 或使用 async with")
        page = await self._context.new_page()
        return page

    async def goto_with_retry(
        self,
        page: Page,
        url: str,
        wait_until: str = "networkidle",
        timeout: int = 30_000,
        retries: int = 3,
    ) -> bool:
        """
        带重试的页面跳转。

        Args:
            page:       Playwright Page 对象
            url:        目标 URL
            wait_until: 等待条件（networkidle / domcontentloaded / load）
            timeout:    单次超时（毫秒）
            retries:    最大重试次数

        Returns:
            True 表示成功，False 表示全部重试失败
        """
        for attempt in range(1, retries + 1):
            try:
                await page.goto(url, wait_until=wait_until, timeout=timeout)
                return True
            except Exception as e:
                if attempt == retries:
                    logger.error("页面加载失败（%d 次重试后）：%s | %s", retries, url, e)
                    return False
                wait_sec = 2 ** attempt  # 指数退避：2s, 4s, 8s
                logger.warning(
                    "页面加载失败（第 %d/%d 次），%.0f 秒后重试：%s",
                    attempt, retries, wait_sec, e,
                )
                await asyncio.sleep(wait_sec)
        return False

    # ------------------------------------------------------------------
    # Cookie 管理
    # ------------------------------------------------------------------

    async def save_cookies(self) -> None:
        """将当前浏览器上下文的 cookie 保存到文件。"""
        if not self.cookie_file or not self._context:
            return
        try:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            cookies = await self._context.cookies()
            with open(self.cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            logger.info("Cookie 已保存：%s（%d 条）", self.cookie_file, len(cookies))
        except Exception as e:
            logger.error("保存 cookie 失败：%s", e)

    async def _load_cookies_from_file(self) -> None:
        """从文件加载 cookie 到浏览器上下文。"""
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if self._context and cookies:
                await self._context.add_cookies(cookies)
                logger.info("Cookie 已加载：%s（%d 条）", self.cookie_file, len(cookies))
        except Exception as e:
            logger.warning("加载 cookie 失败：%s", e)

    async def check_login_status(self, page: Page, logged_in_selector: str) -> bool:
        """
        检查当前页面是否处于登录状态。

        Args:
            page:                Playwright Page 对象
            logged_in_selector:  登录后才会出现的 CSS 选择器（如用户头像、昵称）

        Returns:
            True 表示已登录
        """
        try:
            await page.wait_for_selector(logged_in_selector, timeout=5_000)
            return True
        except Exception:
            return False
