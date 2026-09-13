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
        # 名稱用 data.gov.tw 上的正式名稱。「含外購電力」不是贅字——
        # 統計到的離岸風場（沃一風、海能風等）都是購電，不是台電自有機組。
        "name": "台灣電力公司　各機組發電量即時資訊（含外購電力）",
        "url": "https://service.taipower.com.tw/data/opendata/apply/file/d006001/001.json",
        "page": "https://data.gov.tw/dataset/8931",
        # 台電自己的即時表格頁，數字可以當場逐列核對
        "live_page": "https://dr.taipower.com.tw/d006/loadGraph/loadGraph/genshx_.html",
        # 即時頁面同源的資料檔。比 d006001 多一欄「離岸風力台電自有／購電」，
        # 離岸的判定直接用它，不要自己猜名稱。
        "category_url": "https://dr.taipower.com.tw/d006/loadGraph/loadGraph/data/genary.json",
    },
    "energy": {
        # 能源統計專區 OPEN API 的 4-01 再生能源發電量。這是目前唯一查得到
        # 離岸「發電量」的政府資料——它把風力分成陸域與離岸兩欄。
        # Swagger: https://ea01.moeaea.gov.tw/a0303/02/api/pages/database/api
        "name": "經濟部能源署　能源統計專區 OPEN API《4-01 再生能源發電量》",
        "url": "https://ea01.moeaea.gov.tw/a0303/02/api/v1/zone/monthly/4/1",
        "page": "https://ea01.moeaea.gov.tw/a0303/02/database/api/",
        # 備援：data.gov.tw 登記的年資料 CSV，只有風力合計
        "fallback_name": "經濟部能源署　發電量年資料（再生能源_風力_全國）",
        "fallback_url": "https://www.moeaea.gov.tw/ECW/populace/opendata/wHandOpenData_File.ashx?set_id=70",
        "fallback_page": "https://data.gov.tw/dataset/16481",
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


def fresh_enough(iso, hours):
    """上次算出來還沒過期就沿用，不要每半小時都去翻 55 頁分頁。"""
    if not iso:
        return False
    try:
        age = datetime.now(TPE) - datetime.fromisoformat(iso)
    except ValueError:
        return False
    return age.total_seconds() < hours * 3600


def yi_du(gwh):
    """百萬度（GWh）換算億度，一位小數。1 億度 = 100 GWh。"""
    return round(gwh / 100.0, 1)


# ------------------------------------------------------------ 1. 台電即時
def strip_tags(x):
    return re.sub(r"<[^>]+>", "", str(x or "")).replace("&amp;", "&").strip()


def farm_name(x):
    """「沃四風(註10)」→「沃四風」。尾註不是風場名稱的一部分。"""
    return re.sub(r"\(註\d+\)", "", strip_tags(x)).strip()


def get_taipower():
    """離岸風場即時出力。

    分類直接用台電自己標的「離岸風力台電自有」「離岸風力購電」，不自己猜。
    genary.json（台電即時頁面同源）第 2 欄就是這個分類；data.gov.tw 登記的
    d006001 只有「風力」一層，分不出離岸陸域，所以拿它當備援時才退回名稱比對。
    """
    src = SOURCES["taipower"]
    farms, sample, basis = [], None, None

    try:
        raw = json.loads(decode(fetch(src["category_url"])))
        sample = raw.get("")                      # genary.json 把時間放在空字串鍵
        for r in raw.get("aaData") or []:
            if len(r) < 5 or "離岸風力" not in strip_tags(r[1]):
                continue
            mw, cap = num(strip_tags(r[4])), num(strip_tags(r[3]))
            if mw is None:
                continue
            farms.append({"name": farm_name(r[2]), "mw": round(mw, 1),
                          "cap": round(cap, 1) if cap else None,
                          "owner": "台電自有" if "自有" in strip_tags(r[1]) else "民營購電"})
        basis = "台電官方分類（離岸風力台電自有／購電）"
    except Exception as e:
        print("[warn] genary 分類資料抓不到，改用登記版 d006001：%s" % e)

    if not farms:
        # 備援：d006001 沒有離岸／陸域分類，只能用名稱比對。
        # 這份清單會漏掉之後才併網、名字不在裡面的新風場，所以只當備援。
        raw = json.loads(decode(fetch(src["url"])))
        sample = raw.get("DateTime")
        for r in raw.get("aaData") or []:
            if r.get("機組類型") != "風力":
                continue
            name = (r.get("機組名稱") or "").strip()
            if not name or name.startswith("小計") or not name.startswith(OFFSHORE_PREFIX):
                continue
            mw, cap = num(r.get("淨發電量(MW)")), num(r.get("裝置容量(MW)"))
            if mw is None:
                continue
            farms.append({"name": farm_name(name), "mw": round(mw, 1),
                          "cap": round(cap, 1) if cap else None, "owner": None})
        basis = "名稱比對（備援，台電分類資料無法取得）"

    if not farms:
        raise ValueError("台電回傳裡找不到離岸風力機組")

    farms.sort(key=lambda f: f["mw"], reverse=True)
    total_mw = sum(f["mw"] for f in farms)
    total_cap = sum(f["cap"] for f in farms if f["cap"])
    # 部分新併網風場容量欄位是「-」。容量因數只用有申報容量的機組算，才不會灌水。
    rated_mw = sum(f["mw"] for f in farms if f["cap"])
    owned = [f for f in farms if f.get("owner") == "台電自有"]

    return {
        "status": "ok",
        "sample_time": sample,
        "total_mw": round(total_mw, 1),
        "farm_count": len(farms),
        "owned_count": len(owned) or None,
        "ipp_count": (len(farms) - len(owned)) or None,
        "capacity_mw": round(total_cap, 1),
        "capacity_factor": round(rated_mw / total_cap * 100, 1) if total_cap else None,
        "classification": basis,
        "farms": farms[:8],
        "source": src["name"],
        "source_url": src["page"],
        "live_url": src["live_page"],
    }


# ------------------------------------------------------- 2. 能源署年發電量
YI_MWH = 1e5          # 1 億度 = 100,000 MWh（1 MWh = 1,000 度）


def get_energy():
    """離岸風電年發電量。

    用能源署「能源統計專區 OPEN API」的 4-01 再生能源發電量。這份表把風力
    分成陸域（Column11）與離岸（Column13），是目前唯一查得到離岸「發電量」
    的政府資料——data.gov.tw 上那份 CSV 只有風力合計，分不出離岸。

    抓不到時退回原本的 CSV，屆時只會有合計值，欄位會標成 wind_total。
    """
    src = SOURCES["energy"]
    try:
        return energy_from_api(src)
    except Exception as e:
        print("[warn] 能源統計 API 失敗，退回 data.gov.tw CSV：%s" % e)
        return energy_from_csv(src)


def energy_from_api(src):
    d = json.loads(decode(fetch(src["url"])))
    rows = d.get("再生能源") or []
    key = next((k for k in rows[0] if "再生能源發電量" in k), None) if rows else None
    if not key:
        raise ValueError("能源統計 API 回傳找不到表頭欄位")

    years = []
    for r in rows:
        m = re.fullmatch(r"(\d{2,3})年", str(r.get(key) or "").strip())
        if not m:
            continue                      # 月份列、成長率列都跳過，只留整年
        off, on = r.get("Column13"), r.get("Column11")
        if not isinstance(off, (int, float)):
            continue
        years.append({"year": int(m.group(1)) + 1911, "off": off,
                      "on": on if isinstance(on, (int, float)) else 0})
    if len(years) < 2:
        raise ValueError("能源統計 API 的離岸年度資料不足")
    years.sort(key=lambda x: x["year"])

    growth = []
    for a, b in zip(years, years[1:]):
        if a["off"] > 0:
            growth.append({"year": b["year"],
                           "pct": round((b["off"] / a["off"] - 1) * 100, 2)})

    last = years[-1]
    return {
        "status": "ok",
        "scope": "offshore",
        "year": last["year"],
        "yi": round(last["off"] / YI_MWH, 1),
        "onshore_yi": round(last["on"] / YI_MWH, 1),
        "wind_total_yi": round((last["off"] + last["on"]) / YI_MWH, 1),
        "growth_pct": growth[-1]["pct"] if growth else None,
        "growth_series": growth[-4:],
        "unit_note": "原始單位 MWh，本站換算為億度（1 億度 = 100,000 MWh）",
        "source": src["name"],
        "source_url": src["page"],
    }


def energy_from_csv(src):
    """備援：data.gov.tw 的發電量年資料，只有風力合計、分不出離岸。"""
    rows = list(csv.DictReader(io.StringIO(decode(fetch(src["fallback_url"])))))
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

    growth = [{"year": y, "pct": round((v / pv - 1) * 100, 2)}
              for (py, pv), (y, v) in zip(series, series[1:]) if pv]
    year, gwh = series[-1]
    return {
        "status": "ok",
        "scope": "wind_total",
        "year": year,
        "yi": yi_du(gwh),
        "growth_pct": growth[-1]["pct"] if growth else None,
        "growth_series": growth[-4:],
        "unit_note": "原始單位百萬度，本站換算為億度",
        "source": src["fallback_name"],
        "source_url": src["fallback_page"],
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
def tbn_query(span, limit):
    src = SOURCES["tbn"]
    return "%s?taxonGroup=birds&eventPlaceAdminarea=%s&eventDate=%s&limit=%d" % (
        src["url"], urllib.parse.quote("彰化縣"), urllib.parse.quote(span), limit)


def is_species(name):
    """TBN 的 vernacularName 不一定是單一物種。

    「鳥綱」是只認到綱、「柳鶯屬」只到屬、「小濱鷸; 紅胸濱鷸」是無法二選一的
    存疑紀錄、「大冠鷲(hoya亞種)」會和母種重複計算。這些都不能算成一種鳥。
    """
    if not name or name == "鳥綱" or ";" in name:
        return False
    return not (name.endswith("屬") or name.endswith("科") or "亞種" in name)


def get_tbn_species(span):
    """翻完所有分頁，統計到底有幾種鳥、幾種保育類。

    一次要 55 個請求左右，所以呼叫端每天只跑一次（見 SPECIES_TTL_H）。
    """
    url, seen, prot, fams = tbn_query(span, 1000), {}, {}, set()
    pages = 0
    while url and pages < 120:            # 上限純粹是保險，避免分頁壞掉時無限迴圈
        d = json.loads(decode(fetch(url)))
        for r in d.get("data", []):
            name = (r.get("vernacularName") or "").strip()
            if not is_species(name):
                continue
            seen[name] = seen.get(name, 0) + 1
            if r.get("protectedStatusTW"):
                prot[name] = r["protectedStatusTW"]
            if r.get("familyVernacularName"):
                fams.add(r["familyVernacularName"])
        url = (d.get("links") or {}).get("next")
        pages += 1

    if not seen:
        raise ValueError("TBN 分頁翻完但沒有可用的物種名")

    # 挑幾種上得了檯面的：先瀕臨絕種、再珍貴稀有，同級的看紀錄數
    def rank(item):
        name, level = item
        tier = 0 if "瀕臨絕種" in level else (1 if "珍貴稀有" in level else 2)
        return (tier, -seen.get(name, 0))

    notable = [{"name": n, "records": seen.get(n, 0), "status": lv}
               for n, lv in sorted(prot.items(), key=rank)[:6]]

    return {
        "species_count": len(seen),
        "protected_count": len(prot),
        "family_count": len(fams),
        "notable": notable,
        "species_at": datetime.now(TPE).isoformat(timespec="seconds"),
    }


SPECIES_TTL_H = 24        # 物種組成變化很慢，一天翻一次分頁就夠了

# 每個來源各自的重抓間隔（小時）。排程每 10 分鐘跑一次，但只有到期的才會真的去要。
# 依據是各來源自己的更新頻率，不是我們想要多即時：
#   台電   每 10 分鐘發佈一次即時發電量 → 每次都抓
#   能源署 年資料，一年才動一次        → 一天一次已經太勤
#   iOcean 目擊回報不定期更新，且單檔 250KB → 六小時一次
#   TBN    觀測紀錄是批次上傳的         → 一小時一次
FETCH_TTL_H = {"power_live": 0, "power_year": 24, "sea": 6, "sky": 1}


def due(key, prev):
    ttl = FETCH_TTL_H.get(key, 0)
    if ttl <= 0:
        return True
    old = (prev or {}).get(key) or {}
    if old.get("status") not in ("ok", "stale"):
        return True                       # 上次沒成功，不要等 TTL
    return not fresh_enough(old.get("fetched_at"), ttl)


def get_tbn(prev=None):
    """TBN 觀測紀錄：查風場所在的彰化縣、近一年的鳥類觀測。"""
    src = SOURCES["tbn"]
    today = datetime.now(TPE).date()
    span = "%s~%s" % ((today - timedelta(days=365)).strftime("%Y-%m"),
                      today.strftime("%Y-%m"))

    data = json.loads(decode(fetch(tbn_query(span, 1))))
    # v2.6 把總筆數放在 meta.total；v2.5 以前是頂層的 count。兩種都接。
    total = (data.get("meta") or {}).get("total", data.get("count"))
    if total is None:
        raise ValueError("TBN 回傳找不到總筆數（meta.total / count）")

    out = {
        "status": "ok",
        "recent_12m": int(total),
        "area": "彰化縣",
        "window": "近 12 個月",
        "source": src["name"],
        "source_url": src["page"],
    }

    old = (prev or {}).get("sky") or {}
    if fresh_enough(old.get("species_at"), SPECIES_TTL_H):
        for k in ("species_count", "protected_count", "family_count",
                  "notable", "species_at"):
            if k in old:
                out[k] = old[k]
    else:
        try:
            out.update(get_tbn_species(span))
        except Exception as e:        # 物種統計失敗不該讓整個天空面板掛掉
            print("[warn] TBN 物種統計失敗，沿用舊值：%s" % e)
            for k in ("species_count", "protected_count", "family_count",
                      "notable", "species_at"):
                if k in old:
                    out[k] = old[k]
    return out


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
        if not due(key, prev):
            # 還沒到期就原封不動沿用。這不是失敗，狀態維持 ok，
            # 資料多舊看各區塊自己的 fetched_at。
            out[key] = dict(prev[key])
            print("[skip] %-11s 未到重抓時間（每 %d 小時）"
                  % (key, FETCH_TTL_H[key]))
            continue
        try:
            # 只有 TBN 需要看上一版（決定物種統計要不要重算）
            result = fn(prev) if key == "sky" else fn()
            result["fetched_at"] = datetime.now(TPE).isoformat(timespec="seconds")
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
        "source_url": "https://eiadoc.moenv.gov.tw/eiaweb/",
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
