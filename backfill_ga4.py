"""
One-time GA4 backfill from 2024-01-01 to yesterday.
"""
import json
from datetime import datetime, timedelta, timezone
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import RunReportRequest, DateRange, Metric, Dimension
from google.oauth2 import service_account

TZ        = timezone(timedelta(hours=8))
START     = "2024-01-01"
END       = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

GA4_PROPERTY = "324221264"

GA4_METRICS = [
    Metric(name="sessions"), Metric(name="addToCarts"),
    Metric(name="conversions"), Metric(name="totalRevenue"),
    Metric(name="averageSessionDuration"), Metric(name="bounceRate"),
]
GA4_ITEM_METRICS = [
    Metric(name="itemsViewed"), Metric(name="itemsAddedToCart"),
    Metric(name="itemsPurchased"), Metric(name="itemRevenue"),
]
GA4_SEARCH_METRICS = [Metric(name="eventCount"), Metric(name="sessions")]

def ga4_client():
    creds = service_account.Credentials.from_service_account_file(
        "ga4_sa.json", scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=creds)

def ga4_fetch(client, dimensions, metrics, start_date, end_date, limit=250000):
    resp = client.run_report(RunReportRequest(
        property=f"properties/{GA4_PROPERTY}",
        dimensions=[Dimension(name=n) for n in dimensions],
        metrics=metrics,
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        limit=limit,
    ))
    dim_names    = [d.name for d in resp.dimension_headers]
    metric_names = [m.name for m in resp.metric_headers]
    rows = []
    for row in resp.rows:
        d = {dim_names[i]: row.dimension_values[i].value for i in range(len(dim_names))}
        for i, m in enumerate(metric_names):
            d[m] = row.metric_values[i].value
        rows.append(d)
    return rows

client = ga4_client()
cache = {}

print(f"Fetching daily ({START} ~ {END})...")
cache["daily"] = sorted(ga4_fetch(client, ["date"], GA4_METRICS, START, END), key=lambda r: r["date"])
print(f"  {len(cache['daily'])} rows")

print("Fetching daily_channels...")
cache["daily_channels"] = sorted(ga4_fetch(client, ["date","sessionDefaultChannelGrouping"], GA4_METRICS, START, END), key=lambda r: (r["date"], r["sessionDefaultChannelGrouping"]))
print(f"  {len(cache['daily_channels'])} rows")

print("Fetching daily_source_medium...")
cache["daily_source_medium"] = sorted(ga4_fetch(client, ["date","sessionSourceMedium"], GA4_METRICS, START, END), key=lambda r: (r["date"], r["sessionSourceMedium"]))
print(f"  {len(cache['daily_source_medium'])} rows")

print("Fetching daily_items...")
cache["daily_items"] = sorted(ga4_fetch(client, ["date","itemName"], GA4_ITEM_METRICS, START, END, limit=50000), key=lambda r: (r["date"], r["itemName"]))
print(f"  {len(cache['daily_items'])} rows")

print("Fetching daily_search...")
cache["daily_search"] = sorted(ga4_fetch(client, ["date","searchTerm"], GA4_SEARCH_METRICS, START, END, limit=50000), key=lambda r: (r["date"], r["searchTerm"]))
print(f"  {len(cache['daily_search'])} rows")

cache["updated"] = END

with open("ga4_cache.json", "w") as f:
    json.dump(cache, f, ensure_ascii=False)

print(f"Done. ga4_cache.json updated ({START} ~ {END})")
