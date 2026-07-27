"""
build_dashboard.py — Deen Capital cloud dashboard builder.

Runs in GitHub Actions once a day (or on manual dispatch):
  1. loads paper_state.json (sleeve weights + benchmark levels as of last_mark),
  2. fetches adjusted daily closes via yfinance for every held name + benchmark,
  3. marks NAV forward day-by-day using the SAME daily-return math as the local
     paper_tracker.mark() (buy-and-hold intra-sleeve drift; dividend-inclusive via
     adjusted prices), appending to paper_track.csv,
  4. regenerates a fully self-contained index.html for GitHub Pages.

--offline : skip the price fetch and just rebuild index.html from existing data
            (used for local render tests and as the fetch-failed fallback).

Single source of truth going forward is the cloud record, seeded from the local
state on 2026-07-17. Monthly rebalances are produced locally and synced by updating
data/target_book.json + data/paper_state.json in the repo.
"""
import os, sys, json, math

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE = os.path.join(DATA, "paper_state.json")
TRACK = os.path.join(DATA, "paper_track.csv")
BOOK = os.path.join(DATA, "target_book.json")
STATS = os.path.join(DATA, "tear_sheet_stats.json")
OUT = os.path.join(HERE, "index.html")

BENCH = ["QQQ", "SPUS", "SPY", "HLAL"]

# ---- static track-record block (backtest 2010-2025; changes rarely) ----
PEER_META = {
    "QQQ":  "Nasdaq-100",
    "SPUS": "SP Funds S&P 500 Shariah",
    "SPY":  "S&P 500",
    "HLAL": "Wahed FTSE USA Shariah",
}
BT_ROWS = [
    {"k":"DEEN","t":"Deen Capital","d":"Halal systematic blend","cagr":19.3,"sharpe":0.99,"mdd":-32.5,"vol":19.9,"best":55.6,"worst":-28.8,"g10k":168442,"corr":0.97,"hero":True},
    {"k":"QQQ","t":"QQQ","d":"Invesco Nasdaq-100","cagr":18.6,"sharpe":0.93,"mdd":-35.1,"vol":20.6,"best":54.9,"worst":-32.6,"g10k":152277,"corr":1.00,"hero":False},
    {"k":"SPY","t":"SPY","d":"SPDR S&P 500","cagr":13.9,"sharpe":0.84,"mdd":-33.7,"vol":17.2,"best":32.3,"worst":-18.2,"g10k":80199,"corr":0.88,"hero":False},
    {"k":"SPUS","t":"SPUS ‡","d":"SP Funds S&P 500 Shariah","cagr":17.9,"sharpe":0.87,"mdd":-30.8,"vol":21.6,"best":35.6,"worst":-22.8,"g10k":None,"corr":None,"hero":False},
    {"k":"HLAL","t":"HLAL ‡","d":"Wahed FTSE USA Shariah","cagr":16.2,"sharpe":0.84,"mdd":-33.6,"vol":None,"best":None,"worst":-17.6,"g10k":None,"corr":None,"hero":False},
]
ANNUAL = {
    "years":[2010,2011,2012,2013,2014,2015,2016,2017,2018,2019,2020,2021,2022,2023,2024,2025],
    "DEEN":[18.4,6.7,21.9,26.7,18.7,4.1,5.9,34.9,-4.0,39.0,55.6,27.8,-28.8,44.8,37.4,30.0],
}
SUB = [
    {"label":"2010 – 2019","cagr":16.5,"sharpe":1.03,"mdd":-21.0,"exc":-1.38,"verdict":"Trailed QQQ","good":False},
    {"label":"2020 – 2025","cagr":24.2,"sharpe":0.99,"mdd":-32.5,"exc":3.32,"verdict":"Led QQQ","good":True},
]
SLEEVE_META = [  # display order, labels, colours; targets are canonical
    ("CORE","Core equity",70,"var(--gold)"),
    ("OFF","Momentum tilt",15,"var(--qqq)"),
    ("GLD","Gold",10,"var(--hlal)"),
    ("SPSK","Sukuk",5,"var(--spus)"),
]

MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]


def short_date(iso):
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m)-1]} {int(d)}"


def long_date(iso):
    y, m, d = iso.split("-")
    return f"{MONTHS[int(m)-1]} {int(d)}, {y}"


# ---------------- marking (faithful port of paper_tracker.mark) ----------------
def mark_forward(state, prices):
    """prices: pandas DataFrame of ADJUSTED closes (index=Timestamp, cols=tickers).
    Returns list of new rows [date, nav, QQQ, SPUS, SPY, HLAL]; mutates state in place."""
    import numpy as np
    import pandas as pd
    rets = prices.pct_change()
    last = pd.Timestamp(state["last_mark"])
    days = rets.index[rets.index > last]
    rows = []
    for d in days:
        r = rets.loc[d]
        for s, sl in state["sleeves"].items():
            w = sl["weights"]
            sret = 0.0
            for t in w:
                ri = r.get(t, np.nan) if t in rets.columns else np.nan
                sret += w[t] * (0.0 if not np.isfinite(ri) else ri)
            sl["value"] *= (1.0 + sret)
            nw, tot = {}, 0.0
            for t in w:
                ri = r.get(t, np.nan) if t in rets.columns else np.nan
                nv = w[t] * (1.0 + (0.0 if not np.isfinite(ri) else ri))
                nw[t] = nv
                tot += nv
            sl["weights"] = {t: v / tot for t, v in nw.items()} if tot > 0 else w
        for b in BENCH:
            rb = r.get(b, np.nan) if b in rets.columns else np.nan
            state["bench"][b] *= (1.0 + (0.0 if not np.isfinite(rb) else rb))
        nav = sum(sl["value"] for sl in state["sleeves"].values())
        rows.append([str(d.date()), nav] + [state["bench"][b] for b in BENCH])
    if len(days):
        state["last_mark"] = str(days[-1].date())
    return rows


def fetch_and_mark():
    """Fetch prices via yfinance and append new daily marks. Returns count appended."""
    import numpy as np
    import pandas as pd
    import yfinance as yf

    state = json.load(open(STATE))
    tickers = set(BENCH)
    for s, sl in state["sleeves"].items():
        tickers |= set(sl["weights"].keys())
    tickers = sorted(tickers)

    start = state["last_mark"]  # inclusive; first row seeds pct_change
    print(f"[build] fetching {len(tickers)} tickers from {start} ...")
    raw = yf.download(tickers, start=start, auto_adjust=True, progress=False,
                      threads=True, group_by="column")
    if raw is None or len(raw) == 0:
        print("[build] WARNING: empty price frame — skipping mark")
        return 0
    # Extract adjusted closes, tolerant of either MultiIndex column order
    # ((Field,Ticker) or (Ticker,Field)) and of the single-ticker Series case.
    close = None
    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            close = raw.xs("Close", axis=1, level=0)
        elif "Close" in raw.columns.get_level_values(-1):
            close = raw.xs("Close", axis=1, level=-1)
    if close is None:
        close = raw
    if isinstance(close, pd.Series):
        close = close.to_frame()
    close = close.reindex(columns=tickers)
    close.index = pd.to_datetime(close.index)
    close = close.sort_index()

    missing = [t for t in tickers if t not in close.columns or close[t].notna().sum() == 0]
    if missing:
        print(f"[build] note: no data for {missing} (treated flat)")

    rows = mark_forward(state, close)
    if not rows:
        print("[build] no new trading days since last mark")
        return 0

    tr = pd.read_csv(TRACK)
    merged = (pd.concat([tr, pd.DataFrame(rows, columns=tr.columns)], ignore_index=True)
              .drop_duplicates("date", keep="last").sort_values("date"))
    _atomic_csv(merged, TRACK)
    _atomic_json(state, STATE)
    print(f"[build] appended {len(rows)} daily mark(s); last_mark={state['last_mark']}")
    return len(rows)


def _atomic_json(obj, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def _atomic_csv(df, path):
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


# ---------------- assemble DATA for the page ----------------
def compute_data():
    import csv
    rows = list(csv.DictReader(open(TRACK)))
    dates = [r["date"] for r in rows]
    cols = {"DEEN": "nav", "QQQ": "QQQ", "SPUS": "SPUS", "SPY": "SPY", "HLAL": "HLAL"}
    series = {k: [float(r[c]) for r in rows] for k, c in cols.items()}

    def total(v):
        return (v[-1] / v[0] - 1.0) * 100.0

    def mdd(v):
        peak, m = v[0], 0.0
        for x in v:
            peak = max(peak, x)
            m = min(m, x / peak - 1.0)
        return m * 100.0

    def drets(v):
        return [v[i] / v[i - 1] - 1.0 for i in range(1, len(v))]

    def corr(a, b):
        n = len(a)
        if n < 2:
            return 0.0
        ma, mb = sum(a) / n, sum(b) / n
        cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        va = sum((x - ma) ** 2 for x in a)
        vb = sum((x - mb) ** 2 for x in b)
        return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else 0.0

    dr = drets(series["DEEN"])
    deen_tot = total(series["DEEN"])
    up = sum(1 for x in dr if x > 0)
    peers = []
    for k in BENCH:
        peers.append({
            "k": k, "full": PEER_META[k],
            "ret": round(total(series[k]), 2),
            "spread": round(deen_tot - total(series[k]), 2),
            "dd": round(mdd(series[k]), 2),
            "corr": round(corr(dr, drets(series[k])), 2),
        })

    state = json.load(open(STATE))
    sv = {s: state["sleeves"][s]["value"] for s in state["sleeves"]}
    tot_sv = sum(sv.values()) or 1.0
    sleeves = [{"k": k, "label": lbl, "w": round(sv.get(k, 0) / tot_sv * 100, 1),
                "tgt": tgt, "c": col} for (k, lbl, tgt, col) in SLEEVE_META]

    book = json.load(open(BOOK)).get("target_book", {})
    holdings = sorted(book.items(), key=lambda kv: -kv[1])[:12]
    holdings = [[t, round(w * 100, 2)] for t, w in holdings]
    n_equity = sum(1 for t in book if t not in ("GLD", "SPSK"))

    asof, incep = dates[-1], dates[0]
    return {
        "meta": {
            "asof": asof, "asofShort": short_date(asof), "asofLong": long_date(asof),
            "inception": incep, "inceptionShort": short_date(incep),
            "navStr": "$" + format(round(series["DEEN"][-1]), ",d"),
            "inceptionValStr": "$" + format(round(series["DEEN"][0]), ",d"),
            "navPct": round(deen_tot, 2),
            "tradingDays": len(dates) - 1,
        },
        "paper": {"dates": [short_date(d) for d in dates], "series": series},
        "standings": {
            "deen": {"pct": round(deen_tot, 2), "mdd": round(mdd(series["DEEN"]), 2),
                     "upDays": up, "nDays": len(dr),
                     "worst": round(min(dr) * 100, 2) if dr else 0.0},
            "peers": peers,
        },
        "bt": {"rows": BT_ROWS, "annual": ANNUAL, "sub": SUB},
        "book": {
            "sleeves": sleeves, "holdings": holdings,
            "chips": [[str(n_equity), "equities"], ["100", "-name halal pond"],
                      ["127", "eligible"], ["130", "PIT exclusions"], ["43", "SPUS-certified"]],
        },
    }


def build_html():
    data = compute_data()
    js = "const DATA = " + json.dumps(data, separators=(",", ":")) + ";"
    html = TEMPLATE.replace("/*__DATA__*/", js)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[build] wrote {OUT} ({len(html):,} bytes) — as of {data['meta']['asofLong']}")


TEMPLATE = r"""<title>Deen Capital — Performance Dashboard</title>
<style>
  :root{
    --bg:#0e1512; --surface:#141d19; --surface-2:#1a2621; --raise:#20302a;
    --border:#26352f; --border-soft:#1e2c27;
    --ink:#eef2ef; --ink-2:#b6c4bc; --ink-3:#7f9089;
    --gold:#d4a95f; --gold-bright:#ecc57e; --gold-dim:#8a6e3c;
    --pos:#4ec59a; --pos-dim:#245247; --neg:#e07a5f; --neg-dim:#5a2e26; --warn:#e0b24a;
    --deen:#d4a95f; --qqq:#5aa7d6; --spus:#67c58f; --spy:#c98fd0; --hlal:#e0a24a;
    --shadow:0 1px 0 rgba(255,255,255,.02), 0 12px 30px -18px rgba(0,0,0,.7);
    --serif:ui-serif,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"SF Mono","Cascadia Code","Segoe UI Mono","Roboto Mono",Menlo,monospace;
  }
  @media (prefers-color-scheme: light){
    :root{
      --bg:#f4f1e8; --surface:#fffdf8; --surface-2:#faf6ec; --raise:#f2ecdd;
      --border:#e3dccb; --border-soft:#ece5d5;
      --ink:#18211c; --ink-2:#48564e; --ink-3:#6f7d74;
      --gold:#9a7328; --gold-bright:#b98e37; --gold-dim:#b8a26f;
      --pos:#1f8f66; --pos-dim:#cde8dd; --neg:#c0553a; --neg-dim:#f0d8ce; --warn:#a9791d;
      --deen:#9a7328; --qqq:#2b6f9e; --spus:#1f8f66; --spy:#8b4f94; --hlal:#a9791d;
      --shadow:0 1px 0 rgba(255,255,255,.6), 0 14px 34px -22px rgba(60,50,20,.4);
    }
  }
  :root[data-theme="dark"]{
    --bg:#0e1512; --surface:#141d19; --surface-2:#1a2621; --raise:#20302a;
    --border:#26352f; --border-soft:#1e2c27;
    --ink:#eef2ef; --ink-2:#b6c4bc; --ink-3:#7f9089;
    --gold:#d4a95f; --gold-bright:#ecc57e; --gold-dim:#8a6e3c;
    --pos:#4ec59a; --pos-dim:#245247; --neg:#e07a5f; --neg-dim:#5a2e26; --warn:#e0b24a;
    --deen:#d4a95f; --qqq:#5aa7d6; --spus:#67c58f; --spy:#c98fd0; --hlal:#e0a24a;
    --shadow:0 1px 0 rgba(255,255,255,.02), 0 12px 30px -18px rgba(0,0,0,.7);
  }
  :root[data-theme="light"]{
    --bg:#f4f1e8; --surface:#fffdf8; --surface-2:#faf6ec; --raise:#f2ecdd;
    --border:#e3dccb; --border-soft:#ece5d5;
    --ink:#18211c; --ink-2:#48564e; --ink-3:#6f7d74;
    --gold:#9a7328; --gold-bright:#b98e37; --gold-dim:#b8a26f;
    --pos:#1f8f66; --pos-dim:#cde8dd; --neg:#c0553a; --neg-dim:#f0d8ce; --warn:#a9791d;
    --deen:#9a7328; --qqq:#2b6f9e; --spus:#1f8f66; --spy:#8b4f94; --hlal:#a9791d;
    --shadow:0 1px 0 rgba(255,255,255,.6), 0 14px 34px -22px rgba(60,50,20,.4);
  }
  *{box-sizing:border-box}
  body{background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.5;
    -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;padding:clamp(14px,3.5vw,40px);}
  .wrap{max-width:1120px;margin:0 auto;}
  .num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1;}
  h1,h2,h3{text-wrap:balance;margin:0;}
  a{color:var(--gold);}
  .masthead{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;
    gap:18px 24px;padding-bottom:20px;margin-bottom:26px;border-bottom:1px solid var(--border);}
  .brand{display:flex;flex-direction:column;gap:6px;}
  .brand .mark{font-family:var(--serif);font-size:clamp(1.9rem,5vw,2.7rem);font-weight:600;
    letter-spacing:.01em;line-height:1;color:var(--ink);}
  .brand .mark b{color:var(--gold);font-weight:600;}
  .brand .sub{font-size:.72rem;letter-spacing:.22em;text-transform:uppercase;color:var(--ink-3);}
  .status{display:flex;flex-direction:column;align-items:flex-end;gap:7px;text-align:right;}
  .asof{display:inline-flex;align-items:center;gap:8px;font-size:.78rem;color:var(--ink-2);
    background:var(--surface);border:1px solid var(--border);border-radius:999px;padding:6px 12px;}
  .pulse{width:8px;height:8px;border-radius:50%;background:var(--gold);animation:pulse 2.6s infinite;}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(212,169,95,.5)}70%{box-shadow:0 0 0 7px rgba(212,169,95,0)}100%{box-shadow:0 0 0 0 rgba(212,169,95,0)}}
  .navval{font-family:var(--mono);font-size:.82rem;color:var(--ink-3);}
  .navval b{color:var(--ink);font-weight:600;}
  section{margin:34px 0;}
  .sechead{display:flex;align-items:baseline;gap:12px;margin-bottom:14px;flex-wrap:wrap;}
  .sechead h2{font-family:var(--serif);font-size:1.32rem;font-weight:600;color:var(--ink);}
  .sechead .eyebrow{font-size:.68rem;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);font-family:var(--mono);}
  .sechead .note{font-size:.8rem;color:var(--ink-3);margin-left:auto;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);}
  .pad{padding:clamp(16px,2.4vw,24px);}
  .hero{display:grid;grid-template-columns:1.15fr 2fr;gap:16px;}
  @media(max-width:760px){.hero{grid-template-columns:1fr;}}
  .heroDeen{display:flex;flex-direction:column;justify-content:center;gap:4px;
    background:linear-gradient(160deg,var(--surface-2),var(--surface));position:relative;overflow:hidden;}
  .heroDeen::after{content:"";position:absolute;right:-40px;top:-40px;width:180px;height:180px;
    border:1px solid var(--gold-dim);opacity:.16;transform:rotate(45deg);border-radius:22px;}
  .heroDeen .lbl{font-size:.72rem;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-3);}
  .heroDeen .big{font-family:var(--mono);font-size:clamp(2.4rem,7vw,3.4rem);font-weight:600;line-height:1;letter-spacing:-.01em;}
  .heroDeen .nav{font-family:var(--mono);font-size:1.05rem;color:var(--ink-2);margin-top:2px;}
  .heroDeen .meta{font-size:.78rem;color:var(--ink-3);margin-top:8px;}
  .vsgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
  @media(max-width:460px){.vsgrid{grid-template-columns:1fr;}}
  .vs{background:var(--surface-2);border:1px solid var(--border-soft);border-radius:11px;padding:13px 15px;display:flex;flex-direction:column;gap:3px;}
  .vs .top{display:flex;align-items:center;justify-content:space-between;gap:8px;}
  .vs .tick{font-family:var(--mono);font-weight:600;font-size:.95rem;color:var(--ink);}
  .vs .full{font-size:.7rem;color:var(--ink-3);}
  .vs .peerret{font-family:var(--mono);font-size:1.15rem;font-weight:600;margin-top:2px;}
  .pill{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);font-size:.74rem;font-weight:600;padding:3px 8px;border-radius:999px;white-space:nowrap;}
  .pill.ahead{background:var(--pos-dim);color:var(--pos);}
  .pill.behind{background:var(--neg-dim);color:var(--neg);}
  .pos{color:var(--pos);} .neg{color:var(--neg);}
  .banner{display:flex;gap:12px;align-items:flex-start;background:var(--surface-2);border:1px solid var(--border);
    border-left:3px solid var(--warn);border-radius:11px;padding:13px 16px;margin-top:14px;font-size:.85rem;color:var(--ink-2);}
  .banner b{color:var(--ink);}
  .banner .ic{color:var(--warn);font-size:1.05rem;line-height:1.3;}
  .chartwrap{overflow-x:auto;}
  svg{display:block;width:100%;height:auto;}
  .legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:12px;font-size:.8rem;color:var(--ink-2);}
  .legend span{display:inline-flex;align-items:center;gap:7px;}
  .dot{width:11px;height:3px;border-radius:2px;display:inline-block;}
  .dot.big{height:4px;width:15px;}
  .tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:14px;background:var(--surface);}
  table{border-collapse:collapse;width:100%;min-width:640px;font-size:.9rem;}
  thead th{font-size:.68rem;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);font-weight:600;
    text-align:right;padding:13px 14px;border-bottom:1px solid var(--border);white-space:nowrap;background:var(--surface-2);}
  thead th:first-child{text-align:left;}
  tbody td{padding:12px 14px;text-align:right;border-bottom:1px solid var(--border-soft);
    font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--ink);white-space:nowrap;}
  tbody td:first-child{text-align:left;font-family:var(--sans);}
  tbody tr:last-child td{border-bottom:none;}
  tr.deenrow{background:linear-gradient(90deg,rgba(212,169,95,.10),transparent 70%);}
  tr.deenrow td:first-child{box-shadow:inset 3px 0 0 var(--gold);}
  .rowname{display:flex;flex-direction:column;gap:1px;}
  .rowname .t{font-weight:600;color:var(--ink);}
  .rowname .d{font-size:.72rem;color:var(--ink-3);font-family:var(--sans);}
  .best{color:var(--gold-bright);font-weight:600;}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
  @media(max-width:620px){.grid2{grid-template-columns:1fr;}}
  .sp{padding:16px 18px;}
  .sp .h{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;}
  .sp .h .p{font-family:var(--mono);font-size:1.02rem;font-weight:600;color:var(--ink);}
  .sp .verdict{font-size:.74rem;font-weight:600;padding:3px 9px;border-radius:999px;font-family:var(--mono);}
  .sp .row{display:flex;justify-content:space-between;padding:5px 0;font-size:.86rem;color:var(--ink-2);border-top:1px solid var(--border-soft);}
  .sp .row:first-of-type{border-top:none;}
  .sp .row .v{font-family:var(--mono);color:var(--ink);}
  .bookgrid{display:grid;grid-template-columns:1.3fr 1fr;gap:16px;}
  @media(max-width:720px){.bookgrid{grid-template-columns:1fr;}}
  .sleeve{margin-bottom:15px;}
  .sleevebar{height:15px;border-radius:6px;overflow:hidden;display:flex;border:1px solid var(--border);}
  .sleevebar i{display:block;height:100%;}
  .sleevelbls{display:flex;flex-wrap:wrap;gap:10px 16px;margin-top:11px;font-size:.8rem;color:var(--ink-2);}
  .sleevelbls span{display:inline-flex;align-items:center;gap:6px;}
  .sq{width:10px;height:10px;border-radius:3px;display:inline-block;}
  .holds{display:flex;flex-direction:column;gap:7px;}
  .hold{display:grid;grid-template-columns:52px 1fr auto;align-items:center;gap:10px;font-size:.85rem;}
  .hold .tk{font-family:var(--mono);font-weight:600;color:var(--ink);}
  .hold .track{height:7px;background:var(--surface-2);border-radius:4px;overflow:hidden;}
  .hold .track i{display:block;height:100%;background:linear-gradient(90deg,var(--gold-dim),var(--gold));}
  .hold .w{font-family:var(--mono);color:var(--ink-2);font-size:.8rem;}
  .chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:14px;}
  .chip{font-size:.74rem;font-family:var(--mono);color:var(--ink-2);background:var(--surface-2);border:1px solid var(--border-soft);border-radius:7px;padding:5px 9px;}
  .chip b{color:var(--ink);}
  .disc{margin-top:36px;padding-top:20px;border-top:1px solid var(--border);display:grid;grid-template-columns:1fr 1fr;gap:20px;}
  @media(max-width:620px){.disc{grid-template-columns:1fr;}}
  .disc h3{font-size:.7rem;letter-spacing:.14em;text-transform:uppercase;color:var(--gold);margin-bottom:8px;font-family:var(--mono);}
  .disc p{font-size:.78rem;color:var(--ink-3);line-height:1.6;}
  .foot{margin-top:22px;font-size:.74rem;color:var(--ink-3);text-align:center;line-height:1.7;}
  .tt{position:fixed;pointer-events:none;z-index:20;background:var(--raise);border:1px solid var(--border);
    border-radius:9px;padding:9px 11px;font-size:.76rem;box-shadow:var(--shadow);opacity:0;transition:opacity .1s;min-width:150px;}
  .tt .d{color:var(--ink-3);font-family:var(--mono);margin-bottom:6px;font-size:.72rem;}
  .tt .r{display:flex;justify-content:space-between;gap:16px;font-family:var(--mono);padding:1px 0;}
  .tt .r .k{color:var(--ink-2);} .tt .r .v{color:var(--ink);font-variant-numeric:tabular-nums;}
  .gridln{stroke:var(--border);}
  .axtx{fill:var(--ink-3);font-family:var(--mono);}
  .ln{fill:none;stroke-linejoin:round;stroke-linecap:round;}
  .s-DEEN{stroke:var(--deen);} .s-QQQ{stroke:var(--qqq);} .s-SPUS{stroke:var(--spus);} .s-SPY{stroke:var(--spy);} .s-HLAL{stroke:var(--hlal);}
  .f-DEEN{fill:var(--deen);} .f-QQQ{fill:var(--qqq);} .f-SPUS{fill:var(--spus);} .f-SPY{fill:var(--spy);} .f-HLAL{fill:var(--hlal);}
  .halo{stroke:var(--surface);}
  .gstop{stop-color:var(--gold);}
  .guide{stroke:var(--gold);}
  .bar-pos{fill:var(--pos);} .bar-neg{fill:var(--neg);}
  @media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important;}}
</style>

<div class="wrap">
  <header class="masthead">
    <div class="brand">
      <div class="mark">Deen<b>·</b>Capital</div>
      <div class="sub">Halal Systematic Equity · Performance</div>
    </div>
    <div class="status">
      <span class="asof"><span class="pulse" aria-hidden="true"></span> Paper record · through <span class="num" id="hAsof"></span></span>
      <span class="navval">NAV <b class="num" id="hNav"></b> · inception <span class="num" id="hIncep"></span></span>
    </div>
  </header>

  <section>
    <div class="sechead"><span class="eyebrow">01 — Live</span><h2>Standing vs the field</h2><span class="note" id="liveNote"></span></div>
    <div class="hero">
      <div class="card pad heroDeen">
        <span class="lbl">Deen Capital · since inception</span>
        <span class="big" id="heroPct"></span>
        <span class="nav" id="heroNav"></span>
        <span class="meta" id="heroMeta"></span>
      </div>
      <div class="vsgrid" id="vsgrid"></div>
    </div>
    <div class="banner" id="banner"></div>
  </section>

  <section>
    <div class="sechead"><span class="eyebrow">02 — Trajectory</span><h2>Every $100 since inception</h2><span class="note" id="trajNote"></span></div>
    <div class="card pad">
      <div class="chartwrap"><svg id="lineChart" viewBox="0 0 820 340" role="img" aria-label="Normalized performance since inception"></svg></div>
      <div class="legend" id="lineLegend"></div>
    </div>
  </section>

  <section>
    <div class="sechead"><span class="eyebrow">03 — Track record</span><h2>Backtest 2010–2025</h2><span class="note">Hypothetical · AAOIFI-compliant · daily-marked</span></div>
    <div class="tablewrap">
      <table>
        <thead><tr><th>Strategy / ETF</th><th>CAGR</th><th>Sharpe</th><th>Max DD</th><th>Volatility</th><th>Best yr</th><th>Worst yr</th><th>$10k →</th><th>Corr QQQ</th></tr></thead>
        <tbody id="btBody"></tbody>
      </table>
    </div>
    <p style="font-size:.76rem;color:var(--ink-3);margin-top:10px;">Full 2010–2025 window for Deen, QQQ, SPY. <b>‡</b> SPUS &amp; HLAL launched 2020 / 2019 — shown over their own live history, not the full 16 years, so their figures aren't window-matched to Deen's.</p>
    <div class="grid2" style="margin-top:16px;"><div class="card sp" id="sp0"></div><div class="card sp" id="sp1"></div></div>
    <div class="sechead" style="margin-top:26px;"><span class="eyebrow">Annual</span><h2 style="font-size:1.1rem;">Deen Capital yearly total return</h2></div>
    <div class="card pad"><div class="chartwrap"><svg id="barChart" viewBox="0 0 820 280" role="img" aria-label="Annual total returns 2010 to 2025"></svg></div></div>
  </section>

  <section>
    <div class="sechead"><span class="eyebrow">04 — Positioning</span><h2>What's in the book today</h2><span class="note" id="bookNote"></span></div>
    <div class="bookgrid">
      <div class="card pad">
        <div style="font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);margin-bottom:12px;">Sleeve allocation</div>
        <div class="sleeve"><div class="sleevebar" id="sleeveBar"></div><div class="sleevelbls" id="sleeveLbls"></div></div>
        <div class="chips" id="chips"></div>
      </div>
      <div class="card pad">
        <div style="font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-3);margin-bottom:12px;">Top holdings</div>
        <div class="holds" id="holds"></div>
      </div>
    </div>
  </section>

  <div class="disc">
    <div><h3>Paper record</h3><p>Simulated NAV modeled from daily closing prices — <b>not actual broker fills</b>. Gross of trading costs, spreads and slippage. Short windows carry no statistical meaning. The fundable, real-money record begins Aug 1, 2026.</p></div>
    <div><h3>Backtest</h3><p><b>Hypothetical / backtested</b> performance, 2010–2025. No actual trading. Internally replicated (reconciles to the production engine at r = 0.9998); no financial-statement audit. Gross of blend-level trading costs. AAOIFI-compliant universe, 130 point-in-time exclusions. <b>Past performance does not predict future results.</b></p></div>
  </div>
  <div class="foot" id="foot"></div>
</div>
<div class="tt" id="tt"></div>

<script>
/*__DATA__*/
"use strict";
const D = DATA, M = D.meta;
const fmt=(v,d=2)=>(v>0?"+":"")+v.toFixed(d)+"%";
const COLORS={DEEN:"var(--deen)",QQQ:"var(--qqq)",SPUS:"var(--spus)",SPY:"var(--spy)",HLAL:"var(--hlal)"};
const $=id=>document.getElementById(id);

$("hAsof").textContent=M.asofShort; $("hNav").textContent=M.navStr; $("hIncep").textContent=M.inceptionShort;
$("liveNote").textContent="Simulated NAV · "+M.tradingDays+" trading days";
$("trajNote").textContent="Indexed to 100 · from "+M.inceptionShort;
$("bookNote").textContent="As of "+M.asofShort+", "+M.asof.slice(0,4);

const dh=D.standings.deen;
$("heroPct").textContent=fmt(dh.pct); $("heroPct").className="big "+(dh.pct>=0?"pos":"neg");
$("heroNav").innerHTML=M.navStr+' <span style="color:var(--ink-3)">/ '+M.inceptionValStr+'</span>';
$("heroMeta").innerHTML='Max drawdown <span class="num '+(dh.mdd<0?'neg':'')+'">'+dh.mdd.toFixed(2)+'%</span> · '+dh.upDays+' up / '+dh.nDays+' days · worst day <span class="num">'+dh.worst.toFixed(2)+'%</span>';

$("vsgrid").innerHTML=D.standings.peers.map(p=>{
  const ahead=p.spread>=0;
  return '<div class="vs"><div class="top"><span><span class="tick" style="color:'+COLORS[p.k]+'">'+p.k+'</span></span>'+
    '<span class="pill '+(ahead?'ahead':'behind')+'">'+(ahead?'▲ Deen ahead ':'▼ Deen behind ')+fmt(p.spread)+'</span></div>'+
    '<span class="full">'+p.full+'</span><span class="peerret '+(p.ret>=0?'pos':'neg')+'">'+fmt(p.ret)+'</span>'+
    '<span style="font-size:.72rem;color:var(--ink-3)" class="num">max DD '+p.dd.toFixed(2)+'%</span></div>';
}).join("");

const td=M.tradingDays, noise=td<40;
$("banner").innerHTML='<span class="ic" aria-hidden="true">◆</span><div>'+
  (noise?'<b>'+td+' trading days is statistical noise, not signal.</b> ':'<b>Still early — '+td+' trading sessions in.</b> ')+
  'Deen is a high-beta Nasdaq book (~0.97 correlation to QQQ), so it moves with tech while the broader halal funds cushion in a dip — read short-run standings with that in mind. The record that counts is real money, starting <b>Aug 1, 2026</b>.</div>';

$("btBody").innerHTML=D.bt.rows.map(r=>{
  const c=(v,dec)=>v==null?"—":v.toFixed(dec);
  const g=r.g10k==null?"—":"$"+r.g10k.toLocaleString("en-US");
  return '<tr class="'+(r.hero?'deenrow':'')+'"><td><div class="rowname"><span class="t" '+(r.hero?'style="color:var(--gold)"':'')+'>'+r.t+'</span><span class="d">'+r.d+'</span></div></td>'+
    '<td class="'+(r.hero?'best':'')+'">'+c(r.cagr,1)+'%</td><td class="'+(r.hero?'best':'')+'">'+c(r.sharpe,2)+'</td>'+
    '<td>'+c(r.mdd,1)+'%</td><td>'+(r.vol==null?"—":r.vol.toFixed(1)+'%')+'</td>'+
    '<td>'+(r.best==null?"—":r.best.toFixed(1)+'%')+'</td><td>'+c(r.worst,1)+'%</td><td>'+g+'</td>'+
    '<td>'+(r.corr==null?"—":r.corr.toFixed(2))+'</td></tr>';
}).join("");

D.bt.sub.forEach((s,i)=>{
  $("sp"+i).innerHTML='<div class="h"><span class="p">'+s.label+'</span><span class="verdict" style="background:'+(s.good?'var(--pos-dim)':'var(--neg-dim)')+';color:'+(s.good?'var(--pos)':'var(--neg)')+'">'+s.verdict+'</span></div>'+
    '<div class="row"><span>CAGR</span><span class="v">'+s.cagr.toFixed(1)+'%</span></div>'+
    '<div class="row"><span>Sharpe</span><span class="v">'+s.sharpe.toFixed(2)+'</span></div>'+
    '<div class="row"><span>Max drawdown</span><span class="v">'+s.mdd.toFixed(1)+'%</span></div>'+
    '<div class="row"><span>Excess vs QQQ / yr</span><span class="v" style="color:'+(s.exc>=0?'var(--pos)':'var(--neg)')+'">'+fmt(s.exc)+'</span></div>';
});

$("sleeveBar").innerHTML=D.book.sleeves.map(s=>'<i style="width:'+s.w+'%;background:'+s.c+'"></i>').join("");
$("sleeveLbls").innerHTML=D.book.sleeves.map(s=>'<span><span class="sq" style="background:'+s.c+'"></span>'+s.label+' <b class="num" style="color:var(--ink)">'+s.w.toFixed(1)+'%</b> <span style="color:var(--ink-3)">/ '+s.tgt+'%</span></span>').join("");
$("chips").innerHTML=D.book.chips.map(c=>'<span class="chip"><b>'+c[0]+'</b>'+c[1]+'</span>').join("");
const maxW=D.book.holdings[0][1];
$("holds").innerHTML=D.book.holdings.map(h=>'<div class="hold"><span class="tk">'+h[0]+'</span><span class="track"><i style="width:'+(h[1]/maxW*100).toFixed(0)+'%"></i></span><span class="w">'+h[1].toFixed(2)+'%</span></div>').join("")+'<div style="font-size:.75rem;color:var(--ink-3);margin-top:4px;">full book in the monthly ticket</div>';

$("foot").innerHTML="Deen Capital — internal performance dashboard · for the operator's use, not an offer or solicitation.<br>Auto-updates daily after US market close · data through "+M.asofLong+".";

/* ---------- line chart ---------- */
(function(){
  const W=820,H=340,PADL=44,PADR=54,PADT=20,PADB=34;
  const keys=["QQQ","SPUS","SPY","HLAL","DEEN"];
  const norm={}; keys.forEach(k=>{const s=D.paper.series[k];norm[k]=s.map(v=>v/s[0]*100);});
  let lo=Infinity,hi=-Infinity; keys.forEach(k=>norm[k].forEach(v=>{lo=Math.min(lo,v);hi=Math.max(hi,v);}));
  lo=Math.floor(lo*2)/2-0.3; hi=Math.ceil(hi*2)/2+0.3;
  const n=D.paper.dates.length;
  const X=i=>PADL+i*(W-PADL-PADR)/(n-1);
  const Y=v=>PADT+(hi-v)/(hi-lo)*(H-PADT-PADB);
  const ns="http://www.w3.org/2000/svg", svg=$("lineChart");
  const el=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
  const step=(hi-lo)>6?2:1;
  for(let g=Math.ceil(lo);g<=hi;g+=step){
    svg.appendChild(el("line",{x1:PADL,x2:W-PADR,y1:Y(g),y2:Y(g),"class":"gridln","stroke-width":g===100?1.2:0.7,"stroke-dasharray":g===100?"":"3 4"}));
    const tx=el("text",{x:PADL-8,y:Y(g)+3.5,"text-anchor":"end","class":"axtx","font-size":"11"});tx.textContent=g;svg.appendChild(tx);
  }
  const xi=[0,Math.floor((n-1)/2),n-1];
  xi.forEach((i,j)=>{const tx=el("text",{x:X(i),y:H-10,"text-anchor":j===0?"start":j===2?"end":"middle","class":"axtx","font-size":"11"});tx.textContent=D.paper.dates[i];svg.appendChild(tx);});
  const path=k=>norm[k].map((v,i)=>(i?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1)).join(" ");
  const area=path("DEEN")+" L"+X(n-1)+" "+Y(lo)+" L"+X(0)+" "+Y(lo)+" Z";
  const grad=el("linearGradient",{id:"g1",x1:0,y1:0,x2:0,y2:1});
  grad.appendChild(el("stop",{offset:"0%","class":"gstop","stop-opacity":"0.20"}));
  grad.appendChild(el("stop",{offset:"100%","class":"gstop","stop-opacity":"0"}));
  svg.appendChild(grad);
  svg.appendChild(el("path",{d:area,fill:"url(#g1)",stroke:"none"}));
  keys.forEach(k=>{
    const deen=k==="DEEN";
    svg.appendChild(el("path",{d:path(k),"class":"ln s-"+k,"stroke-width":deen?3:1.6,"stroke-opacity":deen?1:0.72}));
    const yv=norm[k][n-1];
    svg.appendChild(el("circle",{cx:X(n-1),cy:Y(yv),r:deen?4:3,"class":"f-"+k+" halo","stroke-width":1.5}));
    const t=el("text",{x:X(n-1)+7,y:Y(yv)+3.5,"class":"f-"+k,"font-size":deen?"12":"10.5","font-family":"var(--mono)","font-weight":deen?"700":"500"});
    t.textContent=k;svg.appendChild(t);
  });
  $("lineLegend").innerHTML=["DEEN","QQQ","SPUS","SPY","HLAL"].map(k=>'<span><span class="dot '+(k==='DEEN'?'big':'')+'" style="background:'+COLORS[k]+'"></span>'+(k==='DEEN'?'Deen Capital':k)+'</span>').join("");
  const tt=$("tt");
  const guide=el("line",{"class":"guide","stroke-width":1,"stroke-dasharray":"3 3",opacity:0});svg.appendChild(guide);
  function move(ev){
    const r=svg.getBoundingClientRect();
    const px=(ev.touches?ev.touches[0].clientX:ev.clientX)-r.left, sx=px/r.width*W;
    let i=Math.round((sx-PADL)/((W-PADL-PADR)/(n-1))); i=Math.max(0,Math.min(n-1,i));
    guide.setAttribute("x1",X(i));guide.setAttribute("x2",X(i));guide.setAttribute("y1",PADT);guide.setAttribute("y2",H-PADB);guide.setAttribute("opacity",1);
    tt.style.opacity=1;
    tt.innerHTML='<div class="d">'+D.paper.dates[i]+"</div>"+["DEEN","QQQ","SPUS","SPY","HLAL"].map(k=>{
      const ret=norm[k][i]-100;
      return '<div class="r"><span class="k"><span class="dot" style="background:'+COLORS[k]+';margin-right:5px"></span>'+k+'</span><span class="v" style="color:'+(ret>=0?'var(--pos)':'var(--neg)')+'">'+fmt(ret)+'</span></div>';
    }).join("");
    const cx=ev.touches?ev.touches[0].clientX:ev.clientX, cy=ev.touches?ev.touches[0].clientY:ev.clientY;
    tt.style.left=Math.min(cx+14,window.innerWidth-175)+"px"; tt.style.top=(cy+14)+"px";
  }
  svg.addEventListener("mousemove",move);
  svg.addEventListener("touchstart",move,{passive:true});
  svg.addEventListener("touchmove",move,{passive:true});
  svg.addEventListener("mouseleave",()=>{tt.style.opacity=0;guide.setAttribute("opacity",0);});
  svg.addEventListener("touchend",()=>{tt.style.opacity=0;guide.setAttribute("opacity",0);});
})();

/* ---------- annual bars ---------- */
(function(){
  const W=820,H=280,PADL=40,PADR=16,PADT=20,PADB=40;
  const ys=D.bt.annual.years,vals=D.bt.annual.DEEN,n=ys.length;
  const lo=Math.min(...vals,0),hi=Math.max(...vals);
  const yLo=Math.floor(lo/10)*10-2,yHi=Math.ceil(hi/10)*10;
  const ns="http://www.w3.org/2000/svg",svg=$("barChart");
  const el=(t,a)=>{const e=document.createElementNS(ns,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
  const Y=v=>PADT+(yHi-v)/(yHi-yLo)*(H-PADT-PADB);
  const bw=(W-PADL-PADR)/n*0.62, cx=i=>PADL+(i+0.5)*(W-PADL-PADR)/n;
  for(let g=yLo;g<=yHi;g+=10){
    svg.appendChild(el("line",{x1:PADL,x2:W-PADR,y1:Y(g),y2:Y(g),"class":"gridln","stroke-width":g===0?1.2:0.7,"stroke-dasharray":g===0?"":"3 4"}));
    const t=el("text",{x:PADL-7,y:Y(g)+3.5,"text-anchor":"end","class":"axtx","font-size":"10.5"});t.textContent=g+"%";svg.appendChild(t);
  }
  vals.forEach((v,i)=>{
    const pos=v>=0,y0=Y(0),yv=Y(v);
    svg.appendChild(el("rect",{x:cx(i)-bw/2,y:Math.min(y0,yv),width:bw,height:Math.abs(yv-y0),rx:3,"class":pos?"bar-pos":"bar-neg","fill-opacity":0.9}));
    const lab=el("text",{x:cx(i),y:pos?yv-5:yv+13,"text-anchor":"middle","class":pos?"bar-pos":"bar-neg","font-size":"9.5","font-family":"var(--mono)","font-weight":"600"});
    lab.textContent=Math.round(v);svg.appendChild(lab);
    const yl=el("text",{x:cx(i),y:H-12,"text-anchor":"middle","class":"axtx","font-size":"9.5"});
    yl.textContent="'"+String(ys[i]).slice(2);svg.appendChild(yl);
  });
})();
</script>
"""


if __name__ == "__main__":
    offline = "--offline" in sys.argv
    if not offline:
        try:
            fetch_and_mark()
        except Exception as e:
            print(f"[build] price fetch/mark FAILED ({e!r}) — rebuilding HTML from existing data")
    build_html()
    print("[build] done")
