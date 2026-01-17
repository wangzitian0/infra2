"""
End-to-End User Flows (Playwright).

These tests run against a live environment (Staging/Dev) and perform full user journeys.
They are skipped in Production/Read-Only modes.
"""
import os
import pytest
from pathlib import Path
from playwright.async_api import Page, expect

# --- Configuration ---
APP_URL = os.getenv("APP_URL", "http://localhost:3000")
TEST_ENV = os.getenv("TEST_ENV", "staging").lower()

EXPECTED_TXN_COUNT = 15
PARSING_TIMEOUT = 45000

# Skip all tests in this module if we are in PROD
if TEST_ENV == "prod":
    pytest.skip("Skipping E2E write tests in Production", allow_module_level=True)

# Helper to ensure we have a clean URL
def get_url(path):
    return f"{APP_URL.rstrip('/')}{path}"


@pytest.mark.e2e
async def test_dashboard_load(page: Page):
    """
    [Scenario 1] Dashboard Load
    Verify the dashboard loads and displays key elements.
    """
    await page.goto(get_url("/dashboard"))
    
    # Check for title or main header
    # Adjust based on actual page title which might be "Finance Report" or similar
    await expect(page).to_have_title(lambda t: len(t) > 0) 
    
    # Check for presence of navigation or layout
    # Assuming 'nav' or common header exists
    await expect(page.locator("body")).to_be_visible()


@pytest.mark.e2e
async def test_manual_journal_entry_flow(page: Page):
    """
    [Scenario 2] Manual Journal Entry
    1. Navigate to Journal
    2. Open New Entry Form
    3. Fill details (Balanced)
    4. Submit
    5. Verify entry appears in list
    """
    await page.goto(get_url("/journal"))
    
    # 1. Open Form
    # Wait for the account fetch which happens on modal open
    async with page.expect_response("**/api/accounts**"):
        await page.get_by_role("button", name="New Entry").click()
    
    # 2. Wait for modal
    await expect(page.locator("h2", has_text="New Journal Entry")).to_be_visible()
    
    # 3. Fill Header
    await page.get_by_label("Memo *").fill("E2E Test Entry Auto")
    
    # 4. Fill Lines
    # We need to select accounts.
    selects = page.locator("select")
    
    # Account 1
    await selects.nth(0).select_option(index=1)
    # Amount 1
    await page.locator("input[type='number']").nth(0).fill("10.50")
    
    # Account 2
    await selects.nth(2).select_option(index=2)
    # Direction 2 -> Change to CREDIT
    await selects.nth(3).select_option(value="CREDIT")
    # Amount 2
    await page.locator("input[type='number']").nth(1).fill("10.50")
    
    # 5. Check Balance Indicator
    await expect(page.get_by_text("✓ Balanced")).to_be_visible()
    
    # 6. Submit
    # Wait for the POST request and the subsequent re-fetch of the list
    async with page.expect_response("**/api/journal-entries**", method="POST"):
        async with page.expect_response("**/api/journal-entries**", method="GET"):
            await page.get_by_role("button", name="Create Entry").click()
    
    # 7. Verify Result
    # Modal should close
    await expect(page.locator("h2", has_text="New Journal Entry")).not_to_be_visible()
    
    # Entry should be in the list
    await expect(page.get_by_text("E2E Test Entry Auto")).to_be_visible()
    
    # 8. Cleanup (Teardown)
    # Find the row with our entry
    row = page.locator("div", has=page.get_by_text("E2E Test Entry Auto")).first
    
    # Setup dialog handler for confirmation (single use)
    page.once("dialog", lambda dialog: dialog.accept())
    
    # Click delete (we look for the delete button inside that row/context)
    # The delete button is a "Delete" text badge-error or similar.
    # We can scope it to the row to be safe.
    # Note: In the UI code, the delete button is a sibling of the text, inside a flex container.
    # We'll click the button named "Delete" near our text.
    await page.locator("div", has=page.get_by_text("E2E Test Entry Auto")).get_by_role("button", name="Delete").click()
    
    # Verify it's gone (wait for API refresh)
    await expect(page.get_by_text("E2E Test Entry Auto")).not_to_be_visible()


@pytest.mark.e2e
async def test_statement_import_flow(page: Page, repo_root: Path):
    """
    [Scenario 4] Statement Import (PDF)
    1. Generate synthetic PDF
    2. Upload via UI
    3. Verify parsing status
    """
    import subprocess
    import sys
    
    # Use repo_root fixture for robust pathing
    script_path = repo_root / "scripts" / "generate_pdf_fixtures.py"
    output_dir = repo_root / "tmp" / "fixtures"
    
    # 1. Generate PDF
    cmd = [sys.executable, str(script_path), str(output_dir)]
    
    # Run generation
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        pytest.skip(f"Failed to generate PDF fixture: {e.stderr}. Check if reportlab is installed.")

    target_pdf = output_dir / "e2e_dbs_statement.pdf"
    if not target_pdf.exists():
        pytest.fail(f"Generated PDF not found at {target_pdf}")

    # 2. Upload
    await page.goto(get_url("/statements"))
    
    # Set Institution
    await page.get_by_label("Bank / Institution").fill("DBS E2E Test")
    
    # Upload File
    await page.set_input_files("input[type='file']", str(target_pdf))
    
    # 3. Click Upload
    async with page.expect_response("**/api/statements/upload"):
        await page.get_by_role("button", name="Upload & Parse Statement").click()
    
    # 4. Verify List
    await expect(page.get_by_text("DBS E2E Test").first).to_be_visible()
    
    # 5. Deep Verification (Wait for Parsing)
    # The frontend polls every 3s. Parsing might take 10-30s.
    # We wait for the status badge to be 'parsed' or 'needs review' (which implies parsed)
    # Or we can check for the transaction count.
    # The fixture generates 15 transactions.
    # We'll wait for "15 txns" to appear in the row.
    # We assume the row container is what we found earlier.
    row = page.locator("a", has=page.get_by_text("DBS E2E Test")).first
    
    try:
        # Give it up to 45 seconds for LLM parsing
        await expect(row).to_contain_text(f"{EXPECTED_TXN_COUNT} txns", timeout=PARSING_TIMEOUT)
        await expect(row).to_contain_text("parsed", ignore_case=True)
    except AssertionError:
        # If parsing failed or timed out, we might see 'rejected' or still 'parsing'
        # We log/warn but proceed to cleanup to avoid state leak.
        # But strictly for E2E, this should probably fail if we want to guarantee quality.
        # Given "QA" request, we allow failure here.
        raise

    # 6. Cleanup
    # Find the row container again (refresh element handle)
    row = page.locator("a", has=page.get_by_text("DBS E2E Test")).first
    
    # Handle confirmation
    page.once("dialog", lambda dialog: dialog.accept())
    
    # Click delete button (it has title="Delete Statement")
    await row.get_by_title("Delete Statement").click()
    
    # Verify gone
    await expect(page.get_by_text("DBS E2E Test")).not_to_be_visible()


@pytest.mark.e2e
async def test_reports_view(page: Page):
    """
    [Scenario 3] Reports Page
    Verify reports page loads and renders charts/tables.
    """
    await page.goto(get_url("/reports"))
    
    # Check for main report sections
    # Assuming headings like "Balance Sheet", "Income Statement"
    await expect(page.get_by_text("Balance Sheet", exact=False).first).to_be_visible()


@pytest.mark.e2e
async def test_account_deletion_constraint(page: Page):
    """
    [Scenario 5] Account Deletion Constraints (Negative Test)
    1. Create a new Account
    2. Create a Journal Entry using that Account
    3. Try to Delete the Account -> Should Fail/Alert
    4. Delete the Journal Entry
    5. Delete the Account -> Should Success
    """
    # 1. Create Account
    await page.goto(get_url("/accounts"))
    await page.get_by_role("button", name="Add Account").click()
    await page.fill("input[placeholder='Account Name']", "E2E Constraint Test")
    await page.select_option("select", label="ASSET")
    await page.get_by_role("button", name="Create Account").click()
    await expect(page.get_by_text("E2E Constraint Test")).to_be_visible()
    
    # 2. Create Entry
    await page.goto(get_url("/journal"))
    await page.get_by_role("button", name="New Entry").click()
    await page.get_by_label("Memo *").fill("Constraint Test Entry")
    
    # Use the new account
    selects = page.locator("select")
    # Account 1
    await selects.nth(0).select_option(label="E2E Constraint Test")
    await page.locator("input[type='number']").nth(0).fill("100")
    
    # Account 2 (Any other)
    await selects.nth(2).select_option(index=1)
    await selects.nth(3).select_option(value="CREDIT")
    await page.locator("input[type='number']").nth(1).fill("100")
    
    await page.get_by_role("button", name="Create Entry").click()
    await expect(page.get_by_text("Constraint Test Entry")).to_be_visible()
    
    # 3. Try Delete Account
    await page.goto(get_url("/accounts"))
    row = page.locator("div", has=page.get_by_text("E2E Constraint Test")).last
    
    page.once("dialog", lambda dialog: dialog.accept())
    
    # We expect an error alert or notification
    await row.get_by_title("Delete Account").click()
    
    # Verify Error Message
    await expect(page.get_by_text("Failed to delete account")).to_be_visible()
    # Verify Account still exists
    await expect(page.get_by_text("E2E Constraint Test")).to_be_visible()
    
    # 4. Delete Entry
    await page.goto(get_url("/journal"))
    entry_row = page.locator("div", has=page.get_by_text("Constraint Test Entry")).first
    page.once("dialog", lambda dialog: dialog.accept())
    await entry_row.get_by_role("button", name="Delete").click()
    await expect(page.get_by_text("Constraint Test Entry")).not_to_be_visible()
    
    # 5. Delete Account (Success)
    await page.goto(get_url("/accounts"))
    row = page.locator("div", has=page.get_by_text("E2E Constraint Test")).last
    
    page.once("dialog", lambda dialog: dialog.accept())
    await row.get_by_title("Delete Account").click()
    
    # Verify Gone
    await expect(page.get_by_text("E2E Constraint Test")).not_to_be_visible()
