import os

from playwright.async_api import Browser as AsyncBrowser
from playwright.async_api import Page as AsyncPage
from playwright.async_api import expect as async_expect
from playwright.sync_api import Browser as SyncBrowser
from playwright.sync_api import Page as SyncPage
from playwright.sync_api import expect as sync_expect

# Global test-wide timeout (milliseconds), always at least 300s.
DEFAULT_TIMEOUT = max(300_000, int(os.getenv("SWELANCER_PLAYWRIGHT_TIMEOUT_MS", "300000")))


def _patch_browser_class(browser_cls, *, is_async: bool):
    orig_new_context = browser_cls.new_context
    orig_new_page = browser_cls.new_page

    if is_async:

        async def new_context(self, *a, **kw):
            ctx = await orig_new_context(self, *a, **kw)
            ctx.set_default_timeout(DEFAULT_TIMEOUT)
            ctx.set_default_navigation_timeout(DEFAULT_TIMEOUT)
            return ctx

        async def new_page(self, *a, **kw):
            pg = await orig_new_page(self, *a, **kw)
            pg.set_default_timeout(DEFAULT_TIMEOUT)
            pg.set_default_navigation_timeout(DEFAULT_TIMEOUT)
            return pg

    else:

        def new_context(self, *a, **kw):
            ctx = orig_new_context(self, *a, **kw)
            ctx.set_default_timeout(DEFAULT_TIMEOUT)
            ctx.set_default_navigation_timeout(DEFAULT_TIMEOUT)
            return ctx

        def new_page(self, *a, **kw):
            pg = orig_new_page(self, *a, **kw)
            pg.set_default_timeout(DEFAULT_TIMEOUT)
            pg.set_default_navigation_timeout(DEFAULT_TIMEOUT)
            return pg

    browser_cls.new_context = new_context
    browser_cls.new_page = new_page


def _patch_page_goto():
    orig_sync_goto = SyncPage.goto
    orig_async_goto = AsyncPage.goto

    def sync_goto(self, url, *args, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None or timeout < DEFAULT_TIMEOUT:
            kwargs["timeout"] = DEFAULT_TIMEOUT
        return orig_sync_goto(self, url, *args, **kwargs)

    async def async_goto(self, url, *args, **kwargs):
        timeout = kwargs.get("timeout")
        if timeout is None or timeout < DEFAULT_TIMEOUT:
            kwargs["timeout"] = DEFAULT_TIMEOUT
        return await orig_async_goto(self, url, *args, **kwargs)

    SyncPage.goto = sync_goto
    AsyncPage.goto = async_goto


def pytest_configure(config):
    sync_expect.set_options(timeout=DEFAULT_TIMEOUT)
    async_expect.set_options(timeout=DEFAULT_TIMEOUT)
    _patch_browser_class(SyncBrowser, is_async=False)
    _patch_browser_class(AsyncBrowser, is_async=True)
    _patch_page_goto()


def pytest_addoption(parser):
    parser.addoption(
        "--user-tool-trace",
        action="store_true",
        default=False,
        help="Enable tracing of user-tool operations in Playwright tests.",
    )
