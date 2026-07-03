"""
Daily incremental cache update for GitHub Pages.
Fetches last DAYS_BACK days from SHOPLINE API and GA4, merges into existing JSON files.
"""
import json, os, requests
from datetime import datetime, timedelta, timezone
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension
from google.oauth2 import service_account

TZ          = timezone(timedelta(hours=8))
TODAY       = datetime.now(TZ)
DAYS_BACK   = 7   # fetch last 7 days to cover any late-arriving orders

# ── SHOPLINE ─────────────────────────────────────────────────────────────────

SL_TOKEN  = os.environ["SHOPLINE_TOKEN"]
SL_BASE   = "https://open.shopline.io/v1"
SL_HEADS  = {"Authorization": f"Bearer {SL_TOKEN}", "User-Agent": "cache-updater"}

def sl_fetch(start, end):
    orders, previous_id = [], None
    while True:
        params = {"created_after": start, "created_before": end, "per_page": 250}
        if previous_id:
            params["previous_id"] = previous_id
        resp = requests.get(f"{SL_BASE}/orders", headers=SL_HEADS, params=params, timeout=120)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            break
        orders.extend(items)
        if len(items) < 250:
            break
        previous_id = items[-1]["id"]
    return [o for o in orders if not o.get("channel")]

def sl_agg(orders):
    gmv = revenue = cancelled = returns = 0
    order_count = 0
    for o in orders:
        total = (o.get("total") or {}).get("dollars", 0) or 0
        status = o.get("status", "")
        fin    = o.get("financial_status", "")
        gmv += total
        if status == "cancelled":
            cancelled += total
        elif fin == "refunded":
            returns += total; revenue += total
        else:
            revenue += total; order_count += 1
    return {
        "gmv":      round(gmv),
        "revenue":  round(revenue),
        "cancelled":round(cancelled),
        "returns":  round(returns),
        "orders":   order_count,
        "sessions": 0,
        "cvr":      0,
        "aov":      round(gmv / order_count) if order_count else 0,
    }

def update_shopline():
    with open("sl_cache.json") as f:
        cache = json.load(f)

    for i in range(DAYS_BACK, -1, -1):
        d = (TODAY - timedelta(days=i)).strftime("%Y-%m-%d")
        start = f"{d}T00:00:00+08:00"
        end   = f"{d}T23:59:59+08:00"
        try:
            orders = sl_fetch(start, end)
            cache[d] = sl_agg(orders)
            print(f"[SL] {d}: {len(orders)} orders, gmv={cache[d]['gmv']}")
        except Exception as e:
            print(f"[SL] {d} error: {e}")

    with open("sl_cache.json", "w") as f:
        json.dump(cache, f, ensure_ascii=False)

# ── GA4 ──────────────────────────────────────────────────────────────────────

GA4_PROPERTY = "324221264"
GA4_METRICS  = [
    Metric(name="sessions"),
    Metric(name="addToCarts"),
    Metric(name="conversions"),
    Metric(name="totalRevenue"),
    Metric(name="averageSessionDuration"),
    Metric(name="bounceRate"),
]

def ga4_client():
    creds = service_account.Credentials.from_service_account_file(
        "ga4_sa.json", scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=creds)

def ga4_fetch(client, dimensions, start_date, end_date, limit=100000):
    resp = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY}",
        dimensions=[Dimension(name=n) for n in dimensions],
        metrics=GA4_METRICS,
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=limit,
    ))
    dim_names    = [d.name for d in resp.dimension_headers]
    metric_names = ["sessions","addToCarts","conversions","totalRevenue","averageSessionDuration","bounceRate"]
    rows = []
    for row in resp.rows:
        d = {dim_names[i]: row.dimension_values[i].value for i in range(len(dim_names))}
        for i, m in enumerate(metric_names):
            d[m] = row.metric_values[i].value
        rows.append(d)
    return rows

def update_ga4():
    with open("ga4_cache.json") as f:
        cache = json.load(f)

    start_date = (TODAY - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    end_date   = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")

    client = ga4_client()

    # daily
    new_daily = ga4_fetch(client, ["date"], start_date, end_date)
    daily_map = {r["date"]: r for r in cache.get("daily", [])}
    for r in new_daily:
        daily_map[r["date"]] = r
    cache["daily"] = sorted(daily_map.values(), key=lambda r: r["date"])
    print(f"[GA4] daily updated: {len(new_daily)} rows")

    # daily_channels
    new_ch = ga4_fetch(client, ["date", "sessionDefaultChannelGrouping"], start_date, end_date)
    ch_map = {(r["date"], r["sessionDefaultChannelGrouping"]): r for r in cache.get("daily_channels", [])}
    for r in new_ch:
        ch_map[(r["date"], r["sessionDefaultChannelGrouping"])] = r
    cache["daily_channels"] = sorted(ch_map.values(), key=lambda r: (r["date"], r["sessionDefaultChannelGrouping"]))
    print(f"[GA4] daily_channels updated: {len(new_ch)} rows")

    # daily_source_medium
    new_sm = ga4_fetch(client, ["date", "sessionSourceMedium"], start_date, end_date)
    sm_map = {(r["date"], r["sessionSourceMedium"]): r for r in cache.get("daily_source_medium", [])}
    for r in new_sm:
        sm_map[(r["date"], r["sessionSourceMedium"])] = r
    cache["daily_source_medium"] = sorted(sm_map.values(), key=lambda r: (r["date"], r["sessionSourceMedium"]))
    print(f"[GA4] daily_source_medium updated: {len(new_sm)} rows")

    cache["updated"] = end_date
    with open("ga4_cache.json", "w") as f:
        json.dump(cache, f, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Updating SHOPLINE ===")
    update_shopline()
    print("=== Updating GA4 ===")
    update_ga4()
    print("=== Done ===")
