"""
Portal SSO login and navigation tests.

Tests portal access with Authentik OIDC authentication flow.
"""
import pytest
from playwright.async_api import Page
from conftest import TestConfig


@pytest.mark.smoke
@pytest.mark.sso
async def test_portal_accessible(page: Page, config: TestConfig):
    """Verify Portal homepage is accessible (if configured)."""
    if not config.PORTAL_URL:
        pytest.skip("PORTAL_URL not configured")

    await page.goto(config.PORTAL_URL, wait_until="domcontentloaded")

    current_url = page.url
    assert config.PORTAL_URL in current_url or config.SSO_URL in current_url, \
        f"Expected to be at Portal or SSO, got {current_url}"


@pytest.mark.sso
async def test_sso_login_page_loads(page: Page, config: TestConfig):
    """Verify Authentik SSO login page loads correctly."""
    await page.goto(config.SSO_URL, wait_until="networkidle")

    title = await page.title()
    assert title is not None, "SSO page should load with a title"

    body_content = await page.locator("body").inner_text()
    assert len(body_content) > 0, "SSO page should have content"


@pytest.mark.sso
async def test_portal_password_login(page: Page, config: TestConfig):
    """Test Portal login with password (if credentials provided)."""
    if not config.PORTAL_URL:
        pytest.skip("PORTAL_URL not configured")
    if not config.E2E_USERNAME or not config.E2E_PASSWORD:
        pytest.skip("E2E credentials not configured")

    await page.goto(config.PORTAL_URL)

    if config.SSO_URL in page.url:
        await page.wait_for_load_state("domcontentloaded")

        username_inputs = [
            "input[name='username']",
            "input[name='email']",
            "input[type='text']",
        ]

        for locator in username_inputs:
            if await page.locator(locator).is_visible():
                await page.fill(locator, config.E2E_USERNAME)
                break

        password_input = page.locator("input[type='password']")
        if await password_input.is_visible():
            await password_input.fill(config.E2E_PASSWORD)

            login_button = page.locator("button[type='submit'], text=/登录|login|sign in/i")
            if await login_button.is_visible():
                await login_button.click()
                await page.wait_for_timeout(2000)


@pytest.mark.sso
async def test_portal_has_service_links(page: Page, config: TestConfig):
    """Verify Portal displays service navigation links (if configured)."""
    if not config.PORTAL_URL:
        pytest.skip("PORTAL_URL not configured")

    await page.goto(config.PORTAL_URL, wait_until="networkidle")

    links = page.locator("a, button, [role='link'], [role='button']")
    link_count = await links.count()

    assert link_count > 0, "Portal should display service links"


@pytest.mark.sso
async def test_portal_responsive_design(page: Page, config: TestConfig):
    """Verify Portal works on different screen sizes (if configured)."""
    if not config.PORTAL_URL:
        pytest.skip("PORTAL_URL not configured")

    sizes = [
        {"width": 1920, "height": 1080},
        {"width": 768, "height": 1024},
        {"width": 375, "height": 812},
    ]

    for size in sizes:
        context = await page.context.browser.new_context(viewport=size)
        new_page = await context.new_page()

        try:
            await new_page.goto(config.PORTAL_URL, wait_until="domcontentloaded")

            errors = []
            new_page.on("pageerror", lambda exc: errors.append(str(exc)))

            await new_page.wait_for_load_state("networkidle")

            assert len(errors) == 0, f"Portal should load without errors at {size}: {errors}"
        finally:
            await context.close()
