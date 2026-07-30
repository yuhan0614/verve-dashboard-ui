"""
Daily incremental cache update for GitHub Pages.
Fetches last DAYS_BACK days from SHOPLINE API, GA4, and Meta, merges into existing JSON files.
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

def fetch_product_gender_map():
    result = {}
    url = f"{SL_BASE}/products"
    params = {"per_page": 250, "fields": "title_translations,tags"}
    while url:
        resp = requests.get(url, headers=SL_HEADS, params=params, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        for p in (data.get("items") or []):
            name = (p.get("title_translations") or {}).get("zh-hant") or ""
            if not name:
                continue
            tags = p.get("tags") or []
            has_male   = any("男裝" in t for t in tags)
            has_female = any("女裝" in t for t in tags)
            if has_male and not has_female:
                result[name] = "male"
            elif has_female and not has_male:
                result[name] = "female"
            else:
                result[name] = "other"
        last_id = data.get("last_id")
        if not last_id or not data.get("items"):
            break
        url = f"{SL_BASE}/products"
        params = {"per_page": 250, "fields": "title_translations,tags", "previous_id": last_id}
    print(f"[SL] product gender map: {len(result)} products")
    return result

def update_shopline():
    with open("sl_cache.json") as f:
        cache = json.load(f)
    product_gender = fetch_product_gender_map()
    try:
        with open("sl_items_cache.json") as f:
            items_cache = json.load(f)
    except FileNotFoundError:
        items_cache = {}
    try:
        with open("sl_discounts_cache.json") as f:
            disc_cache = json.load(f)
    except FileNotFoundError:
        disc_cache = {}

    for i in range(DAYS_BACK, -1, -1):
        d = (TODAY - timedelta(days=i)).strftime("%Y-%m-%d")
        start = f"{d}T00:00:00+08:00"
        end   = f"{d}T23:59:59+08:00"
        try:
            orders = sl_fetch(start, end)
            cache[d] = sl_agg(orders)

            items_map, disc_map = {}, {}
            for o in orders:
                if o.get("status") == "cancelled":
                    continue
                gender_raw = (o.get("customer_info") or {}).get("gender", "") or ""
                gender = gender_raw if gender_raw in ("male", "female") else "other"
                for item in (o.get("subtotal_items") or []):
                    if item.get("item_type") not in ("Product", "AddonProduct"):
                        continue
                    tr = item.get("title_translations") or {}
                    name = tr.get("zh-hant") or tr.get("en") or "未知商品"
                    qty  = item.get("quantity", 0) or 0
                    rev  = (item.get("total") or {}).get("dollars", 0) or 0
                    if name not in items_map:
                        items_map[name] = {"name": name, "qty": 0, "revenue": 0,
                                           "category": product_gender.get(name, "other"),
                                           "gender": {"male":   {"qty": 0, "revenue": 0},
                                                      "female": {"qty": 0, "revenue": 0},
                                                      "other":  {"qty": 0, "revenue": 0}}}
                    items_map[name]["qty"]     += qty
                    items_map[name]["revenue"] += round(rev)
                    items_map[name]["gender"][gender]["qty"]     += qty
                    items_map[name]["gender"][gender]["revenue"] += round(rev)
                o_rev = (o.get("total") or {}).get("dollars", 0) or 0
                for promo in (o.get("promotion_items") or []):
                    code = promo.get("coupon_code") or ""
                    if not code:
                        continue
                    disc  = (promo.get("discounted_amount") or {}).get("dollars", 0) or 0
                    tr2   = (promo.get("promotion") or {}).get("title_translations") or {}
                    title = tr2.get("zh-hant") or tr2.get("en") or code
                    if code not in disc_map:
                        disc_map[code] = {"code": code, "title": title, "count": 0, "discount": 0, "revenue": 0}
                    disc_map[code]["count"]    += 1
                    disc_map[code]["discount"] += round(disc)
                    disc_map[code]["revenue"]  += round(o_rev)

            items_cache[d] = list(items_map.values())
            disc_cache[d]  = list(disc_map.values())
            print(f"[SL] {d}: {len(orders)} orders, gmv={cache[d]['gmv']}, products={len(items_map)}, coupons={len(disc_map)}")
        except Exception as e:
            print(f"[SL] {d} error: {e}")

    with open("sl_cache.json", "w") as f:
        json.dump(cache, f, ensure_ascii=False)
    with open("sl_items_cache.json", "w") as f:
        json.dump(items_cache, f, ensure_ascii=False)
    with open("sl_discounts_cache.json", "w") as f:
        json.dump(disc_cache, f, ensure_ascii=False)

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

GA4_ITEM_METRICS = [
    Metric(name="itemsViewed"),
    Metric(name="itemsAddedToCart"),
    Metric(name="itemsPurchased"),
    Metric(name="itemRevenue"),
]

def ga4_client():
    creds = service_account.Credentials.from_service_account_file(
        "ga4_sa.json", scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    return BetaAnalyticsDataClient(credentials=creds)

def ga4_fetch(client, dimensions, metrics, start_date, end_date, limit=100000):
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

def update_ga4():
    with open("ga4_cache.json") as f:
        cache = json.load(f)

    start_date = (TODAY - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    end_date   = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")

    client = ga4_client()

    # daily
    new_daily = ga4_fetch(client, ["date"], GA4_METRICS, start_date, end_date)
    daily_map = {r["date"]: r for r in cache.get("daily", [])}
    for r in new_daily:
        daily_map[r["date"]] = r
    cache["daily"] = sorted(daily_map.values(), key=lambda r: r["date"])
    print(f"[GA4] daily updated: {len(new_daily)} rows")

    # daily_channels
    new_ch = ga4_fetch(client, ["date", "sessionDefaultChannelGrouping"], GA4_METRICS, start_date, end_date)
    ch_map = {(r["date"], r["sessionDefaultChannelGrouping"]): r for r in cache.get("daily_channels", [])}
    for r in new_ch:
        ch_map[(r["date"], r["sessionDefaultChannelGrouping"])] = r
    cache["daily_channels"] = sorted(ch_map.values(), key=lambda r: (r["date"], r["sessionDefaultChannelGrouping"]))
    print(f"[GA4] daily_channels updated: {len(new_ch)} rows")

    # daily_source_medium
    new_sm = ga4_fetch(client, ["date", "sessionSourceMedium"], GA4_METRICS, start_date, end_date)
    sm_map = {(r["date"], r["sessionSourceMedium"]): r for r in cache.get("daily_source_medium", [])}
    for r in new_sm:
        sm_map[(r["date"], r["sessionSourceMedium"])] = r
    cache["daily_source_medium"] = sorted(sm_map.values(), key=lambda r: (r["date"], r["sessionSourceMedium"]))
    print(f"[GA4] daily_source_medium updated: {len(new_sm)} rows")

    # daily_items
    new_items = ga4_fetch(client, ["date", "itemName"], GA4_ITEM_METRICS, start_date, end_date, limit=5000)
    item_map = {(r["date"], r["itemName"]): r for r in cache.get("daily_items", [])}
    for r in new_items:
        item_map[(r["date"], r["itemName"])] = r
    cache["daily_items"] = sorted(item_map.values(), key=lambda r: (r["date"], r["itemName"]))
    print(f"[GA4] daily_items updated: {len(new_items)} rows")

    # daily_search
    GA4_SEARCH_METRICS = [Metric(name="eventCount"), Metric(name="sessions")]
    new_search = ga4_fetch(client, ["date", "searchTerm"], GA4_SEARCH_METRICS, start_date, end_date, limit=5000)
    search_map = {(r["date"], r["searchTerm"]): r for r in cache.get("daily_search", [])}
    for r in new_search:
        search_map[(r["date"], r["searchTerm"])] = r
    cache["daily_search"] = sorted(search_map.values(), key=lambda r: (r["date"], r["searchTerm"]))
    print(f"[GA4] daily_search updated: {len(new_search)} rows")

    cache["updated"] = end_date
    with open("ga4_cache.json", "w") as f:
        json.dump(cache, f, ensure_ascii=False)
    # write split files for fast frontend loading
    for key in ["daily","daily_channels","daily_source_medium","daily_items","daily_search"]:
        fname = "ga4_" + key.replace("daily_","").replace("daily","daily") + ".json"
        with open(fname, "w") as f:
            json.dump(cache.get(key, []), f, ensure_ascii=False)
    with open("ga4_meta.json","w") as f:
        json.dump({"updated": cache["updated"]}, f)

# ── META ─────────────────────────────────────────────────────────────────────

META_TOKEN   = os.environ["META_ACCESS_TOKEN"]
META_ACCOUNT = "1417409513448597"
META_BASE    = "https://graph.facebook.com/v20.0"
META_FIELDS  = "spend,impressions,clicks,inline_link_clicks,actions,action_values,cpc,cost_per_inline_link_click,ctr,purchase_roas,cost_per_action_type,date_start,date_stop,campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,reach,frequency"

def _av(lst, t):
    for x in (lst or []):
        if x.get("action_type") == t:
            return float(x.get("value", 0))
    return 0.0

def _parse_meta(row, level):
    spend   = float(row.get("spend", 0) or 0)
    actions = row.get("actions", [])
    avals   = row.get("action_values", [])
    roas_l  = row.get("purchase_roas", [])
    cpa_l   = row.get("cost_per_action_type", [])
    purchase = _av(actions, "purchase")
    cpa = _av(cpa_l, "purchase") or (spend / purchase if purchase else 0)
    r = {
        "date_start": row.get("date_start", ""),
        "spend": spend,
        "impressions": int(row.get("impressions", 0) or 0),
        "clicks": int(row.get("clicks", 0) or 0),
        "link_clicks": int(row.get("inline_link_clicks", 0) or 0),
        "purchase": purchase,
        "purchase_value": _av(avals, "purchase"),
        "add_to_cart": _av(actions, "add_to_cart"),
        "cpc": float(row.get("cpc", 0) or 0),
        "cplc": float(row.get("cost_per_inline_link_click", 0) or 0),
        "ctr": float(row.get("ctr", 0) or 0),
        "cpa": cpa,
        "roas": float(roas_l[0]["value"]) if roas_l else 0.0,
        "reach": int(row.get("reach", 0) or 0),
        "frequency": float(row.get("frequency", 0) or 0),
    }
    if "age"    in row: r["age"]    = row["age"]
    if "gender" in row: r["gender"] = row["gender"]
    if level in ("campaign", "adset", "ad"):
        r["campaign_id"]   = row.get("campaign_id", "")
        r["campaign_name"] = row.get("campaign_name", "")
    if level in ("adset", "ad"):
        r["adset_id"]   = row.get("adset_id", "")
        r["adset_name"] = row.get("adset_name", "")
    if level == "ad":
        r["ad_id"]   = row.get("ad_id", "")
        r["ad_name"] = row.get("ad_name", "")
    return r

def meta_fetch(since, until, level, time_increment=1, breakdowns=None):
    url = f"{META_BASE}/act_{META_ACCOUNT}/insights"
    params = {
        "access_token": META_TOKEN,
        "fields": META_FIELDS,
        "level": level,
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "time_increment": time_increment,
        "limit": 500,
    }
    if breakdowns:
        params["breakdowns"] = breakdowns
    rows = []
    while url:
        r = requests.get(url, params=params, timeout=120)
        data = r.json()
        if "error" in data:
            raise Exception(data["error"]["message"])
        for row in data.get("data", []):
            rows.append(_parse_meta(row, level))
        url = data.get("paging", {}).get("next")
        params = {}
    return rows

def meta_fetch_ad_urls():
    result = {}
    url = f"{META_BASE}/act_{META_ACCOUNT}/ads"
    params = {
        "access_token": META_TOKEN,
        "fields": "name,creative{object_story_spec,thumbnail_url,image_url}",
        "limit": 500,
    }
    while url:
        r = requests.get(url, params=params, timeout=120)
        data = r.json()
        if "error" in data:
            raise Exception(data["error"]["message"])
        for ad in data.get("data", []):
            name = ad.get("name", "")
            creative = ad.get("creative") or {}
            spec = creative.get("object_story_spec") or {}
            dest = None
            if "link_data" in spec:
                dest = spec["link_data"].get("link")
            elif "video_data" in spec:
                dest = (spec["video_data"].get("call_to_action") or {}).get("value", {}).get("link")
            thumb = creative.get("thumbnail_url") or creative.get("image_url")
            if name not in result:
                result[name] = {"url": dest if dest and "{{" not in (dest or "") else None, "thumb": thumb}
        url = data.get("paging", {}).get("next")
        params = {}
    return result

def update_meta():
    try:
        with open("meta_cache.json") as f:
            cache = json.load(f)
    except FileNotFoundError:
        cache = {"daily": [], "campaigns": [], "ads": [], "age": [], "gender": [], "ad_urls": {}}

    start = (TODAY - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")
    end   = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")

    def merge(existing, new_rows, key_fn):
        m = {key_fn(r): r for r in existing}
        for r in new_rows:
            m[key_fn(r)] = r
        return sorted(m.values(), key=key_fn)

    new_daily = meta_fetch(start, end, "account")
    cache["daily"] = merge(cache["daily"], new_daily, lambda r: r["date_start"])
    print(f"[Meta] daily: {len(new_daily)} rows")

    new_camp = meta_fetch(start, end, "campaign")
    cache["campaigns"] = merge(cache["campaigns"], new_camp, lambda r: (r["date_start"], r["campaign_id"]))
    print(f"[Meta] campaigns: {len(new_camp)} rows")

    new_ads = meta_fetch(start, end, "ad")
    cache["ads"] = merge(cache["ads"], new_ads, lambda r: (r["date_start"], r["ad_id"]))
    print(f"[Meta] ads: {len(new_ads)} rows")

    new_age = meta_fetch(start, end, "campaign", breakdowns="age")
    cache["age"] = merge(cache["age"], new_age, lambda r: (r["date_start"], r["campaign_id"], r.get("age","")))
    print(f"[Meta] age: {len(new_age)} rows")

    new_gen = meta_fetch(start, end, "campaign", breakdowns="gender")
    cache["gender"] = merge(cache["gender"], new_gen, lambda r: (r["date_start"], r["campaign_id"], r.get("gender","")))
    print(f"[Meta] gender: {len(new_gen)} rows")

    try:
        cache["ad_urls"] = meta_fetch_ad_urls()
        print(f"[Meta] ad_urls: {len(cache['ad_urls'])} ads")
    except Exception as e:
        print(f"[Meta] ad_urls SKIPPED — {e}")

    cache["updated"] = end
    with open("meta_cache.json", "w") as f:
        json.dump(cache, f, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Updating SHOPLINE ===")
    update_shopline()
    print("=== Updating GA4 ===")
    update_ga4()
    print("=== Updating Meta ===")
    try:
        update_meta()
    except Exception as e:
        print(f"[Meta] SKIPPED — {e}")
    print("=== Done ===")
