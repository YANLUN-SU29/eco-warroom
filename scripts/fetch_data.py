# -*- coding: utf-8 -*-
"""
三鳥牌｜海陸空生態戰情室　資料抓取器

把提案第五章列出的政府開放資料出處，實際抓下來、清洗、算成 data.json。
只用 Python 標準函式庫，不需要 pip install。

    py -3 scripts/fetch_data.py

抓取原則：
  1. 每個來源各自獨立，任何一個掛掉都不會拖垮其他來源。
  2. 抓不到就標 status=unavailable，絕不編造數字。
  3. 上一版 data.json 的成功結果會被保留並標成 stale，讓畫面看得出資料是哪一天的。
"""

import csv
import io
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TPE = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 出處清單
# 與提案書「五、開放資料 API 介接與參考文獻」逐項對應
SOURCES = {
    "taipower": {
        "name": "台灣電力公司　即時各機組發電量",
        "url": "https://service.taipower.com.tw/data/opendata/apply/file/d006001/001.json",
        "page": "https://service.taipower.com.tw/data/opendata/apply/html/d006001.html",
    },
    "energy": {
        "name": "經濟部能源署　發電量年資料（再生能源_風力_全國）",
        "url": "https://www.moeaea.gov.tw/ECW/populace/opendata/wHandOpenData_File.ashx?set_id=70",
        "page": "https://data.gov.tw/dataset/16481",
    },
    "iocean": {
        "name": "海洋委員會海保署　iOcean 海洋生物目擊回報",
        "url": ("https://iocean.oca.gov.tw/oca_datahub/WebService/GetData.ashx"
                "?id=efb09ebd-1191-43be-ab52-80285c61d703"),
        "page": "https://data.gov.tw/dataset/157621",
    },
    "tbn": {
        # TBN 已改版到 v2.6，舊的 v25 端點整個 404（連官方文件自己的範例網址也是）。
        # 現行版本一律以 https://www.tbn.org.tw/data/api 這頁指到的版本為準。
        "name": "農業部　臺灣生物多樣性網絡 TBN 物種觀測 API v2.6",
        "url": "https://www.tbn.org.tw/api/v26/occurrence",
        "page": "https://www.tbn.org.tw/data/api/v26/occurrence",
    },
}

KEY2SRC = {"power_live": "taipower", "power_year": "energy",
           "sea": "iocean", "sky": "tbn"}

# 台電機組名稱裡的離岸風場。清單放在這裡是為了讓人能逐一核對，
# 而不是用一條看不懂的 regex 猜哪些是離岸。
OFFSHORE_PREFIX = ("離岸", "海洋竹南", "海能", "沃", "芳", "允", "中能", "龍")


def fetch(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://data.gov.tw/",
        "Accept": "application/json, text/csv, */*",
        "Accept-Language": "zh-TW,zh;q=0.9",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def decode(raw):
    for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def num(s):
    """把 742.7 / - / 12,203.3 / 76.882% 這類欄位轉成 float，轉不動就回 None。"""
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("%", "")
    if s in ("", "-", "—", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def yi_du(gwh):
    """百萬度（GWh）換算億度，一位小數。1 億度 = 100 GWh。"""
    return round(gwh / 100.0, 1)


# ------------------------------------------------------------ 1. 台電即時
def get_taipower():
    src = SOURCES["taipower"]
    raw = json.loads(decode(fetch(src["url"])))
    rows = raw.get("aaData") or []
    farms, total_mw, total_cap = [], 0.0, 0.0

    for r in rows:
        if r.get("機組類型") != "風力":
            continue
        name = (r.get("機組名稱") or "").strip()
        if not name or name.startswith("小計"):
            continue
        if not name.startswith(OFFSHORE_PREFIX):
            continue
        mw = num(r.get("淨發電量(MW)"))
        cap = num(r.get("裝置容量(MW)"))
        if mw is None:
            continue
        # (註10) 這種尾註不是風場名稱的一部分，拿掉才好讀
        clean = re.sub(r"\(註\d+\)", "", name).strip()
        farms.append({"name": clean, "mw": round(mw, 1),
                      "cap": round(cap, 1) if cap else None})
        total_mw += mw
        if cap:
            total_cap += cap

    if not farms:
        raise ValueError("台電回傳裡找不到離岸風力機組")

    farms.sort(key=lambda f: f["mw"], reverse=True)
    # 部分新併網風場容量欄位是「-」。容量因數只用有申報容量的機組算，才不會灌水。
    rated_mw = sum(f["mw"] for f in farms if f["cap"])
    return {
        "status": "ok",
        "sample_time": raw.get("DateTime"),
        "total_mw": round(total_mw, 1),
        "farm_count": len(farms),
        "capacity_mw": round(total_cap, 1),
        "capacity_factor": round(rated_mw / total_cap * 100, 1) if total_cap else None,
        "farms": farms[:8],
        "source": src["name"],
        "source_url": src["page"],
    }


# ------------------------------------------------------- 2. 能源署年發電量
def get_energy():
    src = SOURCES["energy"]
    text = decode(fetch(src["url"]))
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("能源署 CSV 是空的")

    key_year = next(k for k in rows[0] if "西元年" in k)
    key_wind = next(k for k in rows[0] if "再生能源_風力_全國" in k)

    series = []
    for r in rows:
        y, v = num(r.get(key_year)), num(r.get(key_wind))
        if y and v:
            series.append((int(y), v))
    series.sort()
    if len(series) < 2:
        raise ValueError("能源署 CSV 風力欄位資料不足")

    growth = []
    for (prev_y, prev_v), (y, v) in zip(series, series[1:]):
        if prev_v:
            growth.append({"year": y, "pct": round((v / prev_v - 1) * 100, 2)})

    year, gwh = series[-1]
    return {
        "status": "ok",
        "year": year,
        "gwh": round(gwh, 1),
        "yi": yi_du(gwh),
        "growth_pct": growth[-1]["pct"] if growth else None,
        "growth_series": growth[-4:],
        "unit_note": "原始單位百萬度，本站換算為億度",
        "source": src["name"],
        "source_url": src["page"],
    }


# ------------------------------------------------------------- 3. iOcean
def get_iocean():
    src = SOURCES["iocean"]
    text = decode(fetch(src["url"]))
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        raise ValueError("iOcean CSV 是空的")

    # 第 1 列英文欄名、第 2 列中文欄名，第 3 列起才是資料
    body = rows[2:]
    today = datetime.now(TPE).date()
    cutoff = today - timedelta(days=365)

    recent, white, latest, total = 0, 0, None, 0
    for r in body:
        if len(r) < 6 or r[1].strip() != "A":      # A = 鯨豚
            continue
        total += 1
        species, when = r[2].strip(), r[4].strip()[:10]
        try:
            d = datetime.strptime(when, "%Y-%m-%d").date()
        except ValueError:
            continue
        if latest is None or d > latest:
            latest = d
        if d >= cutoff:
            recent += 1
            if "白海豚" in species:
                white += 1

    return {
        "status": "ok",
        "recent_12m": recent,
        "white_dolphin_12m": white,
        "total_records": total,
        "latest": latest.isoformat() if latest else None,
        "window": "近 12 個月",
        "source": src["name"],
        "source_url": src["page"],
    }


# ---------------------------------------------------------------- 4. TBN
def get_tbn():
    """TBN 觀測紀錄：查風場所在的彰化沿海、近一年的鳥類觀測筆數。"""
    src = SOURCES["tbn"]
    today = datetime.now(TPE).date()
    span = "%s~%s" % ((today - timedelta(days=365)).strftime("%Y-%m"),
                      today.strftime("%Y-%m"))
    url = "%s?taxonGroup=birds&eventPlaceAdminarea=%s&eventDate=%s&limit=1" % (
        src["url"], urllib.parse.quote("彰化縣"), urllib.parse.quote(span))

    data = json.loads(decode(fetch(url)))
    # v2.6 把總筆數放在 meta.total；v2.5 以前是頂層的 count。兩種都接。
    total = (data.get("meta") or {}).get("total", data.get("count"))
    if total is None:
        raise ValueError("TBN 回傳找不到總筆數（meta.total / count）")
    return {
        "status": "ok",
        "recent_12m": int(total),
        "area": "彰化縣",
        "window": "近 12 個月",
        "source": src["name"],
        "source_url": src["page"],
    }


# ---------------------------------------------------------- 抓不到的時候
def failed(key, err):
    src = SOURCES[key]
    return {
        "status": "unavailable",
        "error": "%s: %s" % (type(err).__name__, str(err)[:160]),
        "source": src["name"],
        "source_url": src["page"],
    }


def load_previous():
    try:
        with open(OUT, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def carry_forward(key, fresh, prev):
    """這次沒抓到、但上次有抓到，就沿用舊值並標成 stale，畫面會顯示是哪一天的。"""
    if fresh.get("status") == "ok":
        return fresh
    old = (prev or {}).get(key)
    if not old or old.get("status") == "unavailable":
        return fresh
    kept = dict(old)
    kept["status"] = "stale"
    kept["error"] = fresh.get("error")
    kept.setdefault("last_ok", (prev or {}).get("generated_at"))
    return kept


def main():
    prev = load_previous()
    now = datetime.now(TPE)
    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "updated": now.strftime("%Y-%m-%d %H:%M"),
    }

    for key, fn in (("power_live", get_taipower), ("power_year", get_energy),
                    ("sea", get_iocean), ("sky", get_tbn)):
        try:
            result = fn()
            print("[ok]   %-11s %s" % (key, SOURCES[KEY2SRC[key]]["name"]))
        except Exception as e:          # 任何一站掛掉都要能繼續跑完其他站
            result = failed(KEY2SRC[key], e)
            print("[fail] %-11s %s" % (key, result["error"]))
        out[key] = carry_forward(key, result, prev)

    # 陸地低頻噪音沒有即時 API。這是環評 SoundPLAN 模型的核實值，
    # status 標成 model 就是為了不讓它假裝成即時感測資料。
    out["land"] = {
        "status": "model",
        "value": "0.0",
        "unit": "dB(A)",
        "distance_m": 2000,
        "grade": "無影響或可忽略",
        "source": "環境部《海洋竹南離岸式風力發電計畫（第 6 次變更）環境影響差異分析報告》SoundPLAN 模型",
        "source_url": "https://eiareport.moenv.gov.tw/",
    }

    ok = [k for k in KEY2SRC if out[k]["status"] == "ok"]
    out["live"] = len(ok) > 0
    out["live_count"] = len(ok)
    out["total_count"] = len(KEY2SRC)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("\n寫入 %s（%d/%d 個來源成功）" % (OUT, len(ok), len(KEY2SRC)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
