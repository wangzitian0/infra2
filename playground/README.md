# playground - Yahoo Finance headless test

Purpose: capture the Yahoo Finance behavior that blocks the `query1` quote/crumb
endpoints even when using a headless browser and valid cookies.

## Contents
- `playground/.venv` : earlier local venv
- `playground/.venv-pw` : Playwright venv
- `playground/yahoo_headless_test.py` : headless test script

## Run
```
source playground/.venv-pw/bin/activate
python -m playwright install chromium
python playground/yahoo_headless_test.py
```

## Expected behavior (current)
- `quote` endpoint: 401 or 429
- `crumb` endpoint: 200 (crumb returned)
- `chart` endpoint: 200
- `quote` with crumb: 429

## Curl quick check
```
curl -s -o /dev/null -w 'quote: %{http_code}\n' -H 'User-Agent: Mozilla/5.0' \
  'https://query1.finance.yahoo.com/v7/finance/quote?symbols=AAPL'

curl -s -o /dev/null -w 'chart: %{http_code}\n' -H 'User-Agent: Mozilla/5.0' \
  'https://query2.finance.yahoo.com/v8/finance/chart/AAPL?interval=1d&range=5d'
```
