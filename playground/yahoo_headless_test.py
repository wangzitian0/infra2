from playwright.sync_api import sync_playwright

URLS = {
    "quote": "https://query1.finance.yahoo.com/v7/finance/quote?symbols=AAPL",
    "chart": "https://query2.finance.yahoo.com/v8/finance/chart/AAPL?interval=1d&range=5d",
    "crumb": "https://query1.finance.yahoo.com/v1/test/getcrumb",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        for name, url in URLS.items():
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                status = resp.status if resp else None
                body = (page.content() or "")[:200].replace("\n", " ")
                print(f"{name} status: {status}")
                print(body)
            except Exception as exc:
                print(f"{name} error: {type(exc).__name__}: {exc}")
            print("---")

        # Cookie + crumb flow, then quote with crumb
        resp = page.goto("https://fc.yahoo.com", wait_until="domcontentloaded", timeout=30000)
        print("fc status:", resp.status if resp else None)

        resp = page.goto("https://query1.finance.yahoo.com/v1/test/getcrumb", wait_until="domcontentloaded", timeout=30000)
        crumb = page.text_content("pre") if page.query_selector("pre") else None
        print("crumb status:", resp.status if resp else None, "crumb:", crumb)

        quote_url = "https://query1.finance.yahoo.com/v7/finance/quote"
        resp = context.request.get(quote_url, params={"symbols": "AAPL", "crumb": crumb or ""})
        print("quote status:", resp.status)
        print(resp.text()[:200])

        browser.close()


if __name__ == "__main__":
    run()
