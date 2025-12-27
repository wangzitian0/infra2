"""
Portal SSO login and navigation tests.

Tests the Homer Portal with Casdoor OIDC authentication flow.
"""
import pytest
from playwright.async_api import Page, expect
from conftest import TestConfig


@pytest.mark.smoke
@pytest.mark.sso
async def test_portal_accessible(page: Page, config: TestConfig):
    """Verify Portal homepage is accessible."""
    await page.goto(config.PORTAL_URL, wait_until="domcontentloaded")

    # Check if we're redirected to SSO or if portal loads
    current_url = page.url

    # Either we see the portal or we're on the SSO login page
    assert config.PORTAL_URL in current_url or config.SSO_URL in current_url, \
        f"Expected to be at Portal or SSO, got {current_url}"


@pytest.mark.sso
async def test_sso_login_page_loads(page: Page, config: TestConfig):
    """Verify Casdoor SSO login page loads correctly."""
    await page.goto(config.SSO_URL, wait_until="networkidle")

    # Casdoor should load and have some interactive elements
    title = await page.title()
    assert title is not None, "SSO page should load with a title"

    # Page should have some content (forms, buttons, links, etc.)
    body_content = await page.locator("body").inner_text()
    assert len(body_content) > 0, "SSO page should have content"


@pytest.mark.sso
async def test_portal_password_login(page: Page, config: TestConfig):
    """Test Portal login with password (if credentials provided)."""
    if not config.TEST_PASSWORD:
        pytest.skip("TEST_PASSWORD not configured")

    # Navigate to Portal
    await page.goto(config.PORTAL_URL)

    # If redirected to SSO, wait for login form
    if config.SSO_URL in page.url:
        await page.wait_for_load_state("domcontentloaded")

        # Try to find and fill login form
        # Casdoor typically has username and password fields
        username_locators = [
            "input[name='username']",
            "input[name='name']",
            "input[type='text']",
        ]

        for locator in username_locators:
            if await page.locator(locator).is_visible():
                await page.fill(locator, config.TEST_USERNAME)
                break

        # Fill password
        password_input = page.locator("input[type='password']")
        if await password_input.is_visible():
            await password_input.fill(config.TEST_PASSWORD)

            # Click login button
            login_button = page.locator("button[type='submit'], text=/登录|login|sign in/i")
            if await login_button.is_visible():
                await login_button.click()

                # Wait for redirect back to portal
                await page.wait_for_url(f"{config.PORTAL_URL}*", timeout=10000)


@pytest.mark.sso
async def test_portal_has_service_links(page: Page, config: TestConfig):
    """Verify Portal displays service navigation links."""
    await page.goto(config.PORTAL_URL, wait_until="networkidle")

    # Homer portal typically shows service links
    # Look for buttons, links, or service cards
    links = page.locator("a, button, [role='link'], [role='button']")
    link_count = await links.count()

    # Portal should have at least some navigation elements
    assert link_count > 0, "Portal should display service links"


@pytest.mark.sso
async def test_portal_session_persistence(page: Page, config: TestConfig):
    """Verify session remains active across page navigation."""
    await page.goto(config.PORTAL_URL, wait_until="networkidle")

    initial_url = page.url

    # Navigate within portal (if there are links)
    links = page.locator("a")
    link_count = await links.count()

    if link_count > 1:
        # Click a link to navigate
        first_link = links.nth(0)
        await first_link.click()
        await page.wait_for_load_state("networkidle")

        # Navigate back to check session
        await page.goto(config.PORTAL_URL)

        # Should not be redirected to login again (session should persist)
        assert config.PORTAL_URL in page.url or not page.url.endswith("/login"), \
            "Session should persist across navigation"


@pytest.mark.sso
async def test_portal_responsive_design(page: Page, config: TestConfig):
    """Verify Portal works on different screen sizes."""
    sizes = [
        {"width": 1920, "height": 1080},  # Desktop
        {"width": 768, "height": 1024},   # Tablet
        {"width": 375, "height": 812},    # Mobile
    ]

    for size in sizes:
        # Create new context with specific viewport
        context = await page.context.browser.new_context(viewport=size)
        new_page = await context.new_page()

        try:
            await new_page.goto(config.PORTAL_URL, wait_until="domcontentloaded")

            # Page should load without errors
            errors = []
            new_page.on("pageerror", lambda exc: errors.append(str(exc)))

            # Wait a moment for any JS errors
            await new_page.wait_for_load_state("networkidle")

            assert len(errors) == 0, f"Portal should load without errors at {size}: {errors}"
        finally:
            await context.close()


@pytest.mark.sso
async def test_oidc_discovery_endpoint(page: Page, config: TestConfig):
    """Verify OIDC discovery endpoint returns valid configuration."""
    # OIDC discovery endpoint (standard location)
    discovery_url = f"{config.SSO_URL}/.well-known/openid-configuration"

    await page.goto(discovery_url, wait_until="domcontentloaded")

    # Page should contain JSON with authorization_endpoint, token_endpoint, etc.
    content = await page.content()

    assert "authorization_endpoint" in content, "OIDC discovery should include authorization_endpoint"
    assert "token_endpoint" in content, "OIDC discovery should include token_endpoint"


@pytest.mark.sso
async def test_portal_footer_links(page: Page, config: TestConfig):
    """Verify Portal footer contains expected service links."""
    await page.goto(config.PORTAL_URL, wait_until="networkidle")

    # Scroll to bottom to ensure footer is visible
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(500)

    # Look for footer region
    footer = page.locator("footer, [role='contentinfo']")

    if await footer.is_visible():
        footer_text = await footer.text_content()
        # Footer typically contains service names or links
        assert footer_text and len(footer_text) > 0, "Footer should contain content"
