"""Playwright browser session and page primitives used by the agent tools."""

import asyncio
import re
from pathlib import Path
from typing import Any

from loguru import logger
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

_DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_MAX_LINKS = 300


class BrowserSession:
    """Owns a Chromium browser and exposes the page primitives the tools need."""

    def __init__(
        self,
        *,
        headless: bool = True,
        slow_mo: int = 0,
        timeout_ms: int = 30_000,
        screenshot_dir: Path | None = None,
        user_agent: str | None = None,
        locale: str = "en-GB",
    ):
        self._headless = headless
        self._slow_mo = max(0, int(slow_mo))
        self._timeout_ms = timeout_ms
        self._screenshot_dir = Path(screenshot_dir) if screenshot_dir else None
        self._user_agent = user_agent or _DEFAULT_UA
        self._locale = locale
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            slow_mo=self._slow_mo,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale=self._locale,
            user_agent=self._user_agent,
            java_script_enabled=True,
        )
        self.page = await self._context.new_page()
        self.page.set_default_timeout(self._timeout_ms)
        logger.info("Browser started (headless={}, slow_mo={}ms)", self._headless, self._slow_mo)

    async def stop(self) -> None:
        for kind, target in (
            ("context", self._context),
            ("browser", self._browser),
            ("playwright", self._pw),
        ):
            if target is None:
                continue
            try:
                if kind == "playwright":
                    await target.stop()
                else:
                    await target.close()
            except Exception:
                logger.opt(exception=True).debug("Non-fatal error while closing {}", kind)
        self.page = None
        self._context = None
        self._browser = None
        self._pw = None

    async def __aenter__(self) -> "BrowserSession":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    def _require_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("BrowserSession not started")
        return self.page

    # -------------------------------------------------------------- actions
    async def goto(self, url: str) -> dict[str, str]:
        page = self._require_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        except Exception:  # noqa: BLE001 - best-effort: continue even if the load event is slow
            logger.warning("Timed out waiting for domcontentloaded on {}", url)
        # Give client-side / lazy content time to appear.
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:  # noqa: BLE001 - some sites never reach network idle
            pass
        await self._settle()
        await self._nudge_lazy_content(page)
        return {"url": page.url, "title": await page.title()}

    async def wait(self, ms: int) -> dict[str, str]:
        """Pause, letting lazy content load. Used by the agent's ``wait`` tool."""
        page = self._require_page()
        await asyncio.sleep(max(0, min(int(ms), 10_000)) / 1000.0)
        await self._settle()
        return {"action": f"wait({ms}ms)", "url": page.url, "title": await page.title()}

    async def _nudge_lazy_content(self, page: Page) -> None:
        """Scroll through the page to trigger intersection-observer lazy loads."""
        try:
            await page.evaluate(
                """async () => {
                    const h = document.body ? document.body.scrollHeight : 0;
                    const steps = 5;
                    for (let i = 0; i <= steps; i++) {
                        window.scrollTo(0, (h / steps) * i);
                        await new Promise(r => setTimeout(r, 120));
                    }
                    window.scrollTo(0, 0);
                }"""
            )
        except Exception:
            logger.opt(exception=True).debug("Lazy-load nudge failed")
        await self._settle(delay=0.5)

    async def snapshot(self, max_chars: int = 30_000) -> dict[str, Any]:
        """Return visible text + clickable links + metadata for the model."""
        page = self._require_page()
        text = (await page.evaluate("() => document.body ? document.body.innerText : ''")) or ""
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)

        links = await page.eval_on_selector_all(
            "a[href]",
            """els => els
                .map(e => ({ text: (e.innerText || '').trim().replace(/\\s+/g, ' '), href: e.href }))
                .filter(l => l.href && l.href.startsWith('http'))
            """,
        )
        clean: list[dict[str, str]] = []
        seen: set[str] = set()
        for link in links:
            if link["href"] in seen:
                continue
            seen.add(link["href"])
            clean.append(link)
            if len(clean) >= _MAX_LINKS:
                break

        truncated = len(text) > max_chars
        return {
            "url": page.url,
            "title": await page.title(),
            "text": text[:max_chars] + ("\n...[text truncated]" if truncated else ""),
            "text_truncated": truncated,
            "links": clean,
        }

    async def click(self, selector: str) -> dict[str, str]:
        page = self._require_page()
        locator = page.locator(selector).first
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await locator.click(timeout=8000)
        except Exception:  # noqa: BLE001 - sticky headers/overlays intercept clicks; force through
            logger.opt(exception=True).debug(
                "Normal click intercepted; forcing click on {}", selector
            )
            await locator.click(force=True, timeout=8000)
        await self._settle()
        return {"action": "click", "url": page.url, "title": await page.title()}

    async def click_text(self, text: str) -> dict[str, str]:
        page = self._require_page()
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await locator.click(timeout=8000)
        except Exception:  # noqa: BLE001 - sticky headers/overlays intercept clicks; force through
            logger.opt(exception=True).debug(
                "Normal click intercepted; forcing click on text {}", text
            )
            await locator.click(force=True, timeout=8000)
        await self._settle()
        return {"action": f"click_text({text!r})", "url": page.url, "title": await page.title()}

    async def select_option(self, selector: str, value: str) -> dict[str, str]:
        page = self._require_page()
        await page.locator(selector).first.select_option(value)
        await self._settle()
        return {"action": "select_option", "url": page.url, "title": await page.title()}

    async def fill(self, selector: str, value: str) -> dict[str, str]:
        page = self._require_page()
        await page.locator(selector).first.fill(value)
        await self._settle(delay=0.3)
        return {"action": "fill", "url": page.url, "title": await page.title()}

    async def scroll(self, direction: str) -> dict[str, str]:
        page = self._require_page()
        amount = {"up": -1500, "down": 1500}.get(direction, 0)
        if direction == "top":
            expr = "window.scrollTo(0, 0)"
        elif direction == "bottom":
            expr = "window.scrollTo(0, document.body.scrollHeight)"
        else:
            expr = f"window.scrollBy(0, {amount})"
        await page.evaluate(expr)
        await self._settle(delay=0.4)
        return {"action": f"scroll({direction})", "url": page.url}

    async def screenshot(self, name: str) -> dict[str, str]:
        page = self._require_page()
        if self._screenshot_dir is None:
            return {"saved": False, "error": "no screenshot directory configured"}
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "shot"
        path = self._screenshot_dir / f"{safe}.png"
        await page.screenshot(path=str(path))
        logger.info("Screenshot saved: {}", path)
        return {"saved": True, "path": str(path)}

    # -------------------------------------------------------------- helpers
    async def _settle(self, delay: float = 0.8) -> None:
        await asyncio.sleep(delay)
