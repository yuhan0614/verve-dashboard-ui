"""
One-time Meta backfill from 2024-01-01 to yesterday.
Fetches in 90-day chunks to avoid API timeout.
"""
import json, requests
from datetime import datetime, timedelta, timezone

TZ    = timezone(timedelta(hours=8))
TODAY = datetime.now(TZ)
START = "2024-01-01"
END   = (TODAY - timedelta(days=1)).strftime("%Y-%m-%d")

TOKEN   = open("../backend/.env").read().split("META_ACCESS_TOKEN=")[1].split("\n")[0].strip()
ACCOUNT = "1417409513448597"
BASE    = "https://graph.facebook.com/v20.0"
FIELDS  = "spend,impressions,clicks,inline_link_clicks,actions,action_values,cpc,cost_per_inline_link_click,ctr,purchase_roas,cost_per_action_type,date_start,date_stop,campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,reach,frequency"

def _av(lst, t):
    for x in (lst or []):
        if x.get("action_type") == t: return float(x.get("value", 0))
    return 0.0

def parse(row, level):
    spend = float(row.get("spend", 0) or 0)
    actions, avals = row.get("actions", []), row.get("action_values", [])
    roas_l, cpa_l = row.get("purchase_roas", []), row.get("cost_per_action_type", [])
    purchase = _av(actions, "purchase")
    cpa = _av(cpa_l, "purchase") or (spend / purchase if purchase else 0)
    r = {
        "date_start": row.get("date_start",""),
        "spend": spend, "impressions": int(row.get("impressions",0) or 0),
        "clicks": int(row.get("clicks",0) or 0), "link_clicks": int(row.get("inline_link_clicks",0) or 0),
        "purchase": purchase, "purchase_value": _av(avals,"purchase"),
        "add_to_cart": _av(actions,"add_to_cart"),
        "cpc": float(row.get("cpc",0) or 0), "cplc": float(row.get("cost_per_inline_link_click",0) or 0),
        "ctr": float(row.get("ctr",0) or 0), "cpa": cpa,
        "roas": float(roas_l[0]["value"]) if roas_l else 0.0,
        "reach": int(row.get("reach",0) or 0), "frequency": float(row.get("frequency",0) or 0),
    }
    if "age"    in row: r["age"]    = row["age"]
    if "gender" in row: r["gender"] = row["gender"]
    if level in ("campaign","adset","ad"):
        r["campaign_id"] = row.get("campaign_id",""); r["campaign_name"] = row.get("campaign_name","")
    if level in ("adset","ad"):
        r["adset_id"] = row.get("adset_id",""); r["adset_name"] = row.get("adset_name","")
    if level == "ad":
        r["ad_id"] = row.get("ad_id",""); r["ad_name"] = row.get("ad_name","")
    return r

def fetch(since, until, level, breakdowns=None, time_increment=1):
    url = f"{BASE}/act_{ACCOUNT}/insights"
    params = {"access_token": TOKEN, "fields": FIELDS, "level": level,
              "time_range": f'{{"since":"{since}","until":"{until}"}}',
              "limit": 500}
    if time_increment: params["time_increment"] = time_increment
    if breakdowns: params["breakdowns"] = breakdowns
    rows = []
    while url:
        resp = requests.get(url, params=params, timeout=120).json()
        if "error" in resp: raise Exception(resp["error"]["message"])
        for row in resp.get("data", []): rows.append(parse(row, level))
        url = resp.get("paging", {}).get("next"); params = {}
    return rows

def date_chunks(start, end, days=90):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    while s <= e:
        chunk_end = min(s + timedelta(days=days-1), e)
        yield s.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        s = chunk_end + timedelta(days=1)

cache = {"daily": [], "campaigns": [], "ads": [], "age": [], "gender": [], "ad_urls": {}}

for since, until in date_chunks(START, END, days=90):
    print(f"Fetching account/campaign/age/gender {since} ~ {until}...")
    cache["daily"]     += fetch(since, until, "account")
    cache["campaigns"] += fetch(since, until, "campaign")
    cache["age"]       += fetch(since, until, "campaign", breakdowns="age")
    cache["gender"]    += fetch(since, until, "campaign", breakdowns="gender")

for since, until in date_chunks(START, END, days=90):
    print(f"Fetching ads {since} ~ {until}...")
    cache["ads"]       += fetch(since, until, "ad", time_increment=None)

# fetch ad_urls
print("Fetching ad_urls...")
url = f"{BASE}/act_{ACCOUNT}/ads"
params = {"access_token": TOKEN, "fields": "name,creative{object_story_spec,thumbnail_url,image_url}", "limit": 500}
while url:
    resp = requests.get(url, params=params, timeout=120).json()
    for ad in resp.get("data", []):
        name = ad.get("name","")
        creative = ad.get("creative") or {}
        spec = creative.get("object_story_spec") or {}
        dest = None
        if "link_data" in spec: dest = spec["link_data"].get("link")
        elif "video_data" in spec: dest = (spec["video_data"].get("call_to_action") or {}).get("value",{}).get("link")
        thumb = creative.get("thumbnail_url") or creative.get("image_url")
        if name not in cache["ad_urls"]:
            cache["ad_urls"][name] = {"url": dest if dest and "{{" not in (dest or "") else None, "thumb": thumb}
    url = resp.get("paging", {}).get("next"); params = {}

cache["updated"] = END
with open("meta_cache.json", "w") as f:
    json.dump(cache, f, ensure_ascii=False)
print(f"Done. meta_cache.json: daily={len(cache['daily'])}, campaigns={len(cache['campaigns'])}, ads={len(cache['ads'])}")
