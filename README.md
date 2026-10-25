# Bus Fare Tracker (GitHub Actions + CSV + optional Telegram)

This starter project runs every five minutes, reads bus fares from an **authorized JSON feed**, normalizes the results, appends a timestamped row for each bus to `data/prices.csv`, and optionally sends a Telegram message when a bus's fare reaches the configured target.

**Important:** This is not a RedBus scraper. It does not call RedBus's website, undocumented endpoints, browser APIs, or bypass anti-bot controls. Before connecting RedBus or another booking platform, obtain authorization/a partner feed and use the permitted URL/schema it provides. The example JSON is fake demo data.

## 1. Configure your route

Edit `config.json`:

- `origin`, `destination`: cities to search.
- `journey_date`: `YYYY-MM-DD`.
- `target_price_inr`: optional alert threshold; set to `null` to disable price alerts.
- `query_params`: query parameter names/templates required by your authorized feed. `{origin}`, `{destination}`, `{journey_date}` are replaced automatically.
- `items_path`: dotted path to the array of bus records in the JSON response. Use `""` if the response itself is an array.
- `fields`: map the program's standard field names to actual JSON paths returned by the authorized feed.

Example only: if a permitted feed returns `{"data":{"services":[{"code":"x","operator":{"name":"Demo"},"fare":{"amount":450}}]}}`, use `items_path: "data.services"`; map `bus_id` to `code`, `operator` to `operator.name`, and `fare` to `fare.amount`.

The selected `fare` field must resolve to a scalar number or text such as `"₹ 450"`, not a whole nested object.

## 2. Test locally without any external feed

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python tracker.py --sample
```

This writes two fake bus records to `data/prices.csv`. It makes no network request. Run it again to simulate another history snapshot. The sample booking URLs and fares are illustrative only.

## 3. Add it to GitHub

1. Create a GitHub repository and upload/push this folder.
2. Ensure `.github/workflows/fare-tracker.yml` is on the repository's default branch.
3. In **Settings → Secrets and variables → Actions → Variables**, add `FARE_API_URL` with the HTTPS endpoint supplied by your authorized fare-data provider.
4. If the provider requires a bearer token, add `FARE_API_TOKEN` under **Secrets**. It can be omitted if the provider does not require one.
5. Optional Telegram alerts: add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repository secrets. You can disable alerts by leaving these unset or setting `target_price_inr` to `null`.
6. In **Settings → Actions → General → Workflow permissions**, allow the workflow to read and write repository contents. The workflow also declares `contents: write`.
7. Open **Actions → Bus fare tracker → Run workflow** for a manual test. Once the feed is configured, scheduled runs will use the real authorized feed.

## 4. How persistence works

GitHub-hosted runners are disposable, so the workflow appends rows to `data/prices.csv` and commits it back to the repository. Each usable bus result gets a UTC snapshot timestamp. The `data/alert_state.json` file prevents repeated Telegram messages for the same bus while it remains below the target; it can alert again after its fare rises above the target and later drops below it.

**Scale warning:** five-minute polling can mean up to 288 runs and history commits per day. This is suitable only for small personal experiments; it adds Git history rapidly. For long-running tracking, keep the same tracker logic but write snapshots to a database or spreadsheet instead of committing each poll. GitHub schedule times are not guaranteed to start exactly on every five-minute boundary, and the scheduled workflow runs from the default branch.

## 5. Source limitations

This starter expects an authorized endpoint returning JSON. It does not infer an endpoint from a booking page. Adapt `query_params`, `items_path`, and `fields` to match the feed's actual documentation. If the provider returns HTML, needs a browser challenge, or prohibits automated collection, do not try to route around it; request approved API/partner access or use a source that permits scheduled retrieval.
