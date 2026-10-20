#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOPEA-SIGNAALI
==============
Hakee hopeaan liittyvät markkinaindikaattorit ilmaisista rajapinnoista, laskee
niistä läpinäkyvän nousu / neutraali / lasku -jakauman kolmelle aikajänteelle
(päiväkauppa, 1–2 viikkoa, 1–3 kuukautta), muuntaa sen historiallisesti
toteutuneeksi todennäköisyydeksi, kirjaa ennusteet signaalilokiin ja kirjoittaa
tuloksen yhdeksi HTML-sivuksi (sopii suoraan GitHub Pagesiin).

Käyttö:
    python hopea.py                 # yksi ajo  -> site/index.html
    python hopea.py --loop 60       # päivitä 60 s välein (selain päivittyy itse)
    python hopea.py --demo          # keksityllä datalla, ei verkkoyhteyttä
    python hopea.py --out docs/index.html --json docs/data.json
    python hopea.py --log-dir loki  # signaaliloki (oletus loki/), --no-log = ei lokia

Datalähteet (kaikki ilmaisia, ei API-avaimia):
    Yahoo Finance (yfinance)  hopea, kulta, kupari, dollari-indeksi, EUR/USD, 10v korko
    FRED (St. Louis Fed)      USA:n 10 v reaalikorko, sarja DFII10
    CFTC                      Commitments of Traders, hopea (COMEX), Managed Money

Ei sijoitusneuvontaa. Luvut kuvaavat indikaattorien yksimielisyyttä, eivät
takuuta hinnan suunnasta.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ════════════════════════════════════════════════════════════════════════════
#  ASETUKSET – näitä voi muokata vapaasti
# ════════════════════════════════════════════════════════════════════════════

TIMEZONE = "Europe/Helsinki"     # sivulla näytettävät kellonajat
SIGNAL_THRESHOLD = 0.85          # "kauppasignaalin" raja (85 %)
STRONG_THRESHOLD = 0.70          # "selvä kallistuma"
HISTORY_PERIOD = "10y"           # päivädatan pituus (historiatestiä varten)
INTRADAY_PERIOD = "60d"          # päivänsisäisen datan pituus (Yahoo: 15 min dataa enintään 60 pv)
INTRADAY_INTERVAL = "15m"        # kynttilän pituus päivänsisäisessä analyysissä
DEFAULT_PAGE_REFRESH = 300       # sivun automaattinen uudelleenlataus (s)
DAY_AHEAD_MINUTES = 240          # päiväkaupan aikajänne: hinta 4 tunnin päästä
LOG_DIR = "loki"                 # signaalilokin kansio
LOG_DAILY_FILE = "signaaliloki_paiva.csv"
LOG_HOURLY_FILE = "signaaliloki_tunti.csv"
CALL_THRESHOLD = 0.20            # seurannassa "ennuste" = nousu − lasku vähintään ±20 %-yks

# Välimuistin elinajat silmukka-ajossa (sekunteina)
TTL_INTRADAY = 0                 # aina tuore
TTL_DAILY = 300
TTL_FRED = 3600
TTL_COT = 6 * 3600

TICKERS = {
    "silver": "SI=F",            # COMEX-hopeafutuuri (lähin)
    "gold": "GC=F",              # COMEX-kultafutuuri
    "copper": "HG=F",            # COMEX-kuparifutuuri
    "usd": "DX-Y.NYB",           # Dollari-indeksi DXY
    "eurusd": "EURUSD=X",        # euron hinta näyttöä varten
    "us10y": "^TNX",             # nimelliskorko – varalla jos FRED ei vastaa
}
FRED_REAL_YIELD = "DFII10"
COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
COT_SILVER_CODE = "084691"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (hopea-signaali; +https://github.com)"}

TROY_OUNCE_G = 31.1034768

# Indikaattorit: avain, nimi, ryhmä, selitys ja lyhyet ilmaisut yhteenvetoon.
# Painot ovat aikajännekohtaisia (HORIZONS alempana).
INDICATORS = [
    dict(key="gsr", name="Kulta–hopea-suhde", group="Arvostus",
         what="Kertoo, montako unssia hopeaa saa yhdellä kultaunssilla. Korkea luku "
              "tarkoittaa, että hopea on kultaan nähden halpaa – ja halpa hopea on "
              "historiallisesti usein kirinyt kultaa kiinni.",
         pos="hopean edullisuus kultaan nähden", neg="hopean kalleus kultaan nähden",
         formula="s = tanh( 0,5·z(suhteen taso vs. 3 v) − 0,5·z(suhteen 20 pv muutos) )"),
    dict(key="usd", name="Dollari (DXY)", group="Makro",
         what="Dollarin arvo muihin päävaluuttoihin nähden. Hopea hinnoitellaan "
              "dollareissa, joten heikkenevä dollari tekee hopeasta halvempaa muille "
              "ostajille ja nostaa yleensä sen hintaa.",
         pos="heikkenevä dollari", neg="vahvistuva dollari",
         formula="s = −tanh( 0,5·z(20 pv muutos) + 0,5·z(etäisyys 50 pv keskiarvosta) )"),
    dict(key="real_yield", name="Reaalikorko (USA 10 v)", group="Makro",
         what="Inflaation ylittävä korko, jonka turvallinen valtionlaina maksaa. Hopea "
              "ei maksa korkoa, joten kun reaalikorko nousee, hopean pitäminen käy "
              "kalliimmaksi ja sen hinta yleensä laskee.",
         pos="laskevat reaalikorot", neg="nousevat reaalikorot",
         formula="s = −tanh( 0,7·z(20 pv muutos) + 0,3·z(taso vs. 1 v keskiarvo) )"),
    dict(key="copper", name="Kupari", group="Teollisuus",
         what="Kupari mittaa teollisuuden ja talouskasvun vireyttä. Yli puolet hopeasta "
              "käytetään teollisuudessa (aurinkopaneelit, elektroniikka, sähköautot), "
              "joten kuparin nousu kertoo kasvavasta kysynnästä.",
         pos="kuparin nousu", neg="kuparin heikkous",
         formula="s = tanh( 0,5·z(20 pv muutos) + 0,5·z(etäisyys 50 pv keskiarvosta) )"),
    dict(key="gold", name="Kulta", group="Jalometallit",
         what="Kulta vetää koko jalometallimarkkinaa. Hopea seuraa kullan suuntaa, "
              "mutta liikkuu tyypillisesti selvästi jyrkemmin.",
         pos="kullan nousu", neg="kullan heikkous",
         formula="s = tanh( 0,5·z(20 pv muutos) + 0,5·z(etäisyys 50 pv keskiarvosta) )"),
    dict(key="cot", name="Rahastojen positiot (COT)", group="Sijoittajat",
         what="Suurten spekulatiivisten rahastojen nettopositio hopeafutuureissa "
              "(CFTC:n viikkoraportti). Toimii vastavirtaan: kun lähes kaikki ovat jo "
              "ostaneet, uusia ostajia on vähän ja laskuriski kasvaa – ja päinvastoin.",
         pos="rahastojen varovainen positio (ostovaraa jäljellä)",
         neg="rahastojen ylikuumentunut ostopositio",
         formula="s = −(2p − 1)³, missä p = nettoposition sijoitus (0…1) viimeisten "
                 "3 vuoden viikkoihin verrattuna"),
    dict(key="trend", name="Hopean trendi", group="Tekninen",
         what="Hopean oma lyhyen aikavälin suunta: paljonko hinta on muuttunut kahdessa "
              "viikossa ja onko se 20 päivän keskiarvonsa ylä- vai alapuolella.",
         pos="hopean nouseva trendi", neg="hopean laskeva trendi",
         formula="s = tanh( 0,5·z(10 pv muutos) + 0,5·z(etäisyys 20 pv keskiarvosta) )"),
    dict(key="long_trend", name="Pitkä trendi (200 pv)", group="Tekninen",
         what="Hopean suunta kuukausien mittakaavassa: paljonko hinta on muuttunut kolmessa "
              "kuukaudessa ja onko se 200 päivän keskiarvonsa ylä- vai alapuolella. Raaka-aineissa "
              "pitkät trendit ovat tutkimusten mukaan tapana jatkua kuukausia.",
         pos="hopean pitkä nousutrendi", neg="hopean pitkä laskutrendi",
         formula="s = tanh( 0,5·z(60 pv muutos) + 0,5·z(etäisyys 200 pv keskiarvosta) )"),
    dict(key="rsi", name="Ylikuumeneminen (RSI)", group="Tekninen",
         what="Mittaa asteikolla 0–100, onko hinta liikkunut liian nopeasti. Yli 70: "
              "noussut liikaa, korjausliike todennäköisempi. Alle 30: myyty liikaa, "
              "pomppu todennäköisempi. Välillä 30–70 ei signaalia.",
         pos="ylimyyty tilanne (pomppuvaraa)", neg="ylikuumentunut hintanousu",
         formula="RSI > 70: s = −min(1, (RSI − 70)/15) · RSI < 30: s = +min(1, (30 − RSI)/15) "
                 "· muuten s = 0"),
    dict(key="intraday", name="Päivänsisäinen vire", group="Päiväkauppa",
         what="Suunta 15 minuutin kynttilöistä: onko nopea liukuva keskiarvo hitaan "
              "yläpuolella ja onko hinta vuorokauden volyymipainotetun keskihinnan "
              "(VWAP) ylä- vai alapuolella.",
         pos="päivänsisäinen nousuvire", neg="päivänsisäinen laskuvire",
         formula="s = tanh( 0,5·z(EMA9/EMA21 − 1) + 0,5·z(hinta/VWAP − 1) ), z suhteessa "
                 "viimeisten 5 päivän vaihteluun"),
]
# Kulta–hopea-suhteen historialliset vyöhykkeet (pitkän historian mittakaava).
# Lähteet: Money Metals Exchange (keskiarvo 1971– noin 60, 1/1980 ≈ 17, 2/1991 ≈ 100,
# 3/2020 ≈ 125, 4/2025 ≈ 103) ja Gold & Co (2011 hieman yli 30, 4/2025 ≈ 105).
GSR_ZONES = [
    dict(key="z100", lo=100, hi=None, range="yli 100", title="Poikkeuksellisen halpaa", tint="bull2",
         text="Hyvin harvinaista, nähty vain kriiseissä: helmikuu 1991 (≈ 100), maaliskuu 2020 "
              "(≈ 125, kaikkien aikojen ennätys) ja huhtikuu 2025 (yli 100). Kaikkien jälkeen hopea on "
              "myöhemmin kirinyt kultaa voimakkaasti kiinni, mutta pohjan ajoittaminen on vaikeaa."),
    dict(key="z90", lo=90, hi=100, range="90–100", title="Hyvin halpaa", tint="bull",
         text="Hopea on kultaan nähden harvinaisen edullista. Tälle alueelle suhde on yleensä "
              "noussut kriisien tai pörssilaskujen aikana ja viipynyt vain kuukausia."),
    dict(key="z80", lo=80, hi=90, range="80–90", title="Halpaa", tint="bull",
         text="Perinteinen raja, jonka yläpuolella pitkän aikavälin sijoittajat ovat lisänneet "
              "hopeaa tai vaihtaneet kultaa hopeaan."),
    dict(key="z70", lo=70, hi=80, range="70–80", title="Hieman keskitason yläpuolella", tint="none",
         text="Hopea on keskimääräistä hieman edullisempaa, mutta ei vielä selvästi halpaa."),
    dict(key="z60", lo=60, hi=70, range="60–70", title="Pitkän aikavälin keskitaso", tint="none",
         text="Vuodesta 1971 suhde on ollut keskimäärin noin 60. Tällä alueella kumpikaan metalli "
              "ei ole selvästi toista halvempi."),
    dict(key="z0", lo=None, hi=60, range="alle 60", title="Hopea kallista", tint="bear",
         text="Tyypillistä hopean kuumille nousuvaiheille: tammikuu 1980 (≈ 17, Huntin veljesten "
              "hopeakupla) ja huhtikuu 2011 (≈ 31). Näiltä tasoilta hopean lisänousu kultaan nähden on "
              "historiallisesti ollut vaikeampaa."),
]


def gsr_zone(v: float) -> dict | None:
    if v is None or not math.isfinite(v):
        return None
    for z in GSR_ZONES:
        if (z["lo"] is None or v >= z["lo"]) and (z["hi"] is None or v < z["hi"]):
            return z
    return None


IND_BY_KEY = {d["key"]: d for d in INDICATORS}

# Aikajänteet. Jokaisella oma painotus ja oma testiaikajänne (forward = havaintoja eteenpäin).
# Painot ovat kunkin aikajänteen sisällä yhteensä 1. Mittari voi kuulua useaan aikajänteeseen.
HORIZONS = [
    dict(key="day", name="Päiväkauppa", span="seuraavat tunnit", ahead="noin 4 tunnin päästä",
         ahead_short="4 h", forward=16, unit="havaintoa", unit_short="hav.",
         weights={"intraday": 0.60, "trend": 0.25, "rsi": 0.15},
         note="Päivänsisäinen vire 15 minuutin kynttilöistä. Trendi ja RSI edellisen päivän "
              "päätöskurssista, jotta historiatestissä ei käytetä tulevaa tietoa."),
    dict(key="swing", name="Lyhyt", span="1–2 viikkoa", ahead="5 kaupankäyntipäivän päästä",
         ahead_short="5 pv", forward=5, unit="päivää", unit_short="pv",
         weights={"usd": 0.20, "real_yield": 0.20, "copper": 0.15, "gold": 0.15,
                  "gsr": 0.10, "trend": 0.10, "cot": 0.05, "rsi": 0.05},
         note="Makrotausta ja lyhyet trendit päätöskursseista."),
    dict(key="long", name="Pitkä", span="1–3 kuukautta", ahead="40 kaupankäyntipäivän päästä",
         ahead_short="40 pv", forward=40, unit="päivää", unit_short="pv",
         weights={"gsr": 0.35, "long_trend": 0.25, "cot": 0.20, "real_yield": 0.20},
         note="Arvostus, sijoittajien asemoituminen ja pitkä trendi."),
]
HZ = {h["key"]: h for h in HORIZONS}
MAIN_HORIZON = "swing"
CSV_PREFIX = {"day": "paivakauppa", "swing": "lyhyt", "long": "pitka"}
NY = ZoneInfo("America/New_York")

log = logging.getLogger("hopea")


# ════════════════════════════════════════════════════════════════════════════
#  APUFUNKTIOT
# ════════════════════════════════════════════════════════════════════════════

def fi(x: float, d: int = 1, sign: bool = False) -> str:
    """Suomalainen numeromuoto: desimaalipilkku, välilyönti tuhaterottimena."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "–"
    s = f"{x:+,.{d}f}" if sign else f"{x:,.{d}f}"
    return s.replace(",", " ").replace(".", ",").replace("-", "−")


def pct(x: float, d: int = 1, sign: bool = False) -> str:
    return fi(x * 100, d, sign) + " %"


def bar_minutes() -> int:
    v = INTRADAY_INTERVAL
    return int(v[:-1]) * 60 if v.endswith("h") else int(v.rstrip("m"))


def trade_dates(idx) -> pd.DatetimeIndex:
    """Futuurin kauppapäivä: sessio alkaa klo 18 NY-aikaa ja kuuluu seuraavaan päivään."""
    idx = pd.DatetimeIndex(idx)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return (idx.tz_convert(NY) + pd.Timedelta(hours=6)).normalize().tz_localize(None)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=max(2, n // 2)).mean()


def roll_std(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=max(10, n // 2)).std()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def trend_z(price: pd.Series, n_ret: int = 20, n_ema: int = 50, win: int = 252):
    """Trendin voimakkuus z-lukuna: muutos + etäisyys keskiarvosta,
    kumpikin suhteutettuna omaan tavanomaiseen vaihteluunsa."""
    ret = price / price.shift(n_ret) - 1
    gap = price / ema(price, n_ema) - 1
    z = 0.5 * ret / roll_std(ret, win) + 0.5 * gap / roll_std(gap, win)
    return z, ret, gap


def align_asof(series: pd.Series, idx: pd.DatetimeIndex, lag_days: int) -> pd.Series:
    """Siirtää sarjan siihen päivään, jolloin tieto oli oikeasti saatavilla."""
    s = series.dropna().copy()
    s.index = s.index + pd.Timedelta(days=lag_days)
    s = s[~s.index.duplicated(keep="last")]
    return s.reindex(s.index.union(idx)).ffill().reindex(idx)


def last_valid(s: pd.Series, max_age: int = 7):
    v = s.dropna()
    if v.empty:
        return np.nan
    if len(s) - s.index.get_loc(v.index[-1]) - 1 > max_age:
        return np.nan
    return float(v.iloc[-1])


def to_list(s, nd: int = 4):
    out = []
    for v in (s.tolist() if hasattr(s, "tolist") else list(s)):
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            out.append(None)
        else:
            out.append(round(float(v), nd))
    return out


def dates(idx) -> list[str]:
    return [t.strftime("%Y-%m-%d") for t in idx]


# Päivämäärät suomalaisittain ilman etunollia. Ei käytetä strftime("%-d"),
# koska se ei toimi Windowsissa ("Invalid format string").
def pv(t) -> str:            # 20.10.2026
    return f"{t.day}.{t.month}.{t.year}"


def pvk(t) -> str:           # 20.10. klo 02.45
    return f"{t.day}.{t.month}. klo {t.hour:02d}.{t.minute:02d}"


def pvvk(t) -> str:          # 20.10.2026 klo 02.45
    return f"{pv(t)} klo {t.hour:02d}.{t.minute:02d}"


def local_times(idx, tz: ZoneInfo) -> list[str]:
    idx = pd.DatetimeIndex(idx)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return [t.strftime("%Y-%m-%d %H:%M") for t in idx.tz_convert(tz)]


def join_fi(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " ja " + items[-1]


def effect_of(s: float) -> tuple[str, str]:
    """Pistemäärästä selkokielinen vaikutus ja sävy."""
    if s is None or not math.isfinite(s):
        return "Ei dataa", "na"
    a = abs(s)
    if a < 0.15:
        return "Neutraali", "neutral"
    strength = "lievä" if a < 0.40 else "selvä" if a < 0.75 else "vahva"
    return (f"Tukee nousua · {strength}", "bull") if s > 0 else (f"Tukee laskua · {strength}", "bear")


# ════════════════════════════════════════════════════════════════════════════
#  DATAN HAKU
# ════════════════════════════════════════════════════════════════════════════

_CACHE: dict[str, tuple[float, object]] = {}


def cached(key: str, ttl: float, fn):
    """Yksinkertainen välimuisti silmukka-ajoon. Jos haku epäonnistuu ja vanha
    arvo on olemassa, käytetään vanhaa (ja kirjataan varoitus)."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception:
        if hit:
            log.warning("%s: haku epäonnistui, käytetään edellistä dataa", key)
            return hit[1]
        raise
    _CACHE[key] = (now, val)
    return val


def _yahoo(ticker: str, period: str, interval: str) -> pd.DataFrame:
    import yfinance as yf
    err = None
    for attempt in range(3):
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval,
                                           auto_adjust=False, actions=False)
            if df is not None and len(df):
                df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
                df = df[df["Close"].notna()]
                return df[~df.index.duplicated(keep="last")].sort_index()
            err = "tyhjä vastaus"
        except Exception as e:  # noqa: BLE001
            err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Yahoo {ticker}: {err}")


def _yahoo_daily(ticker: str) -> pd.DataFrame:
    df = _yahoo(ticker, HISTORY_PERIOD, "1d")
    df.index = pd.to_datetime([t.date() for t in df.index])  # pörssin paikallinen päivä
    return df[~df.index.duplicated(keep="last")]


def _yahoo_intraday(ticker: str) -> pd.DataFrame:
    df = _yahoo(ticker, INTRADAY_PERIOD, INTRADAY_INTERVAL)
    idx = pd.DatetimeIndex(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return df


def _fred(series: str) -> pd.Series:
    import requests
    key = os.environ.get("FRED_API_KEY")
    if key:  # virallinen API, jos avain on annettu ympäristömuuttujana
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": series, "api_key": key, "file_type": "json",
                                 "observation_start": "2010-01-01"}, timeout=30)
        r.raise_for_status()
        obs = r.json()["observations"]
        s = pd.Series([pd.to_numeric(o["value"], errors="coerce") for o in obs],
                      index=pd.to_datetime([o["date"] for o in obs]))
    else:    # avaimeton CSV-lataus
        r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}",
                         timeout=30, headers=HTTP_HEADERS)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                      index=pd.to_datetime(df.iloc[:, 0]))
    s = s.dropna().sort_index()
    if s.empty:
        raise RuntimeError(f"FRED {series}: tyhjä sarja")
    return s


def _cot() -> pd.DataFrame:
    import requests
    wheres = [f"cftc_contract_market_code='{COT_SILVER_CODE}'",
              "upper(market_and_exchange_names) like 'SILVER - COMMODITY EXCHANGE%'"]
    rows = []
    for where in wheres:
        r = requests.get(COT_URL, params={"$where": where, "$limit": "800",
                                          "$order": "report_date_as_yyyy_mm_dd DESC"},
                         timeout=30, headers=HTTP_HEADERS)
        r.raise_for_status()
        rows = r.json()
        if rows:
            break
    if not rows:
        raise RuntimeError("CFTC: hopean COT-dataa ei löytynyt")
    df = pd.DataFrame(rows)
    cols = {c.lower(): c for c in df.columns}

    def col(*names):
        for n in names:
            if n in cols:
                return df[cols[n]]
        raise KeyError(names)

    out = pd.DataFrame({
        "long": pd.to_numeric(col("m_money_positions_long_all")),
        "short": pd.to_numeric(col("m_money_positions_short_all")),
        "oi": pd.to_numeric(col("open_interest_all")),
    })
    out.index = pd.to_datetime(col("report_date_as_yyyy_mm_dd")).dt.tz_localize(None).dt.normalize()
    out = out[~out.index.duplicated(keep="first")].sort_index()
    out["net"] = out["long"] - out["short"]
    out["net_pct"] = out["net"] / out["oi"] * 100
    return out


@dataclass
class MarketData:
    daily: dict[str, pd.DataFrame]
    intraday: pd.DataFrame | None
    real_yield: pd.Series | None
    real_yield_label: str
    cot: pd.DataFrame | None
    demo: bool = False
    errors: list[str] = field(default_factory=list)


def fetch_all() -> MarketData:
    errors: list[str] = []
    daily: dict[str, pd.DataFrame] = {}
    for key, t in TICKERS.items():
        try:
            daily[key] = cached(f"d:{t}", TTL_DAILY, lambda t=t: _yahoo_daily(t))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t}: {e}")
            log.warning("Päivädata %s epäonnistui: %s", t, e)
    if "silver" not in daily:
        raise RuntimeError("Hopean hintaa ei saatu haettua – sivua ei päivitetä.")

    intraday = None
    try:
        intraday = cached("i:silver", TTL_INTRADAY, lambda: _yahoo_intraday(TICKERS["silver"]))
    except Exception as e:  # noqa: BLE001
        errors.append(f"Päivänsisäinen data: {e}")

    ry, ry_label = None, "USA 10 v reaalikorko (FRED DFII10)"
    try:
        ry = cached("fred", TTL_FRED, lambda: _fred(FRED_REAL_YIELD))
    except Exception as e:  # noqa: BLE001
        errors.append(f"FRED: {e}")
        if "us10y" in daily:
            ry = daily["us10y"]["Close"]
            ry_label = "USA 10 v nimelliskorko (varalla, FRED ei vastannut)"

    cot = None
    try:
        cot = cached("cot", TTL_COT, _cot)
    except Exception as e:  # noqa: BLE001
        errors.append(f"CFTC COT: {e}")

    return MarketData(daily, intraday, ry, ry_label, cot, False, errors)


# ════════════════════════════════════════════════════════════════════════════
#  DEMODATA (testaukseen ilman verkkoa)
# ════════════════════════════════════════════════════════════════════════════

def demo_data(seed: int = 14) -> MarketData:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=2520)
    n = len(idx)
    f_usd = rng.normal(0, 0.0042, n)
    f_pm = rng.normal(0, 0.0075, n)
    f_grow = rng.normal(0, 0.008, n)
    # loppuun nousuvoittoinen jakso, jotta demo näyttää kiinnostavalta
    tail = slice(n - 45, n)
    f_usd[tail] -= 0.0011
    f_pm[tail] += 0.0022
    f_grow[tail] += 0.0016

    usd_r = f_usd
    gold_r = f_pm - 0.45 * f_usd + rng.normal(0, 0.004, n)
    cop_r = f_grow - 0.35 * f_usd + rng.normal(0, 0.008, n)
    sil_r = 1.3 * f_pm + 0.4 * f_grow - 0.45 * f_usd + rng.normal(0, 0.0065, n)

    def path(start, r, end):
        lp = np.cumsum(r)  # satunnaiskulku, joka "sidotaan" uskottavaan loppuarvoon
        lp = lp + np.linspace(0, np.log(end / start) - lp[-1], n)
        return pd.Series(start * np.exp(lp), index=idx)

    closes = {
        "silver": path(16.5, sil_r, 47.0), "gold": path(1280.0, gold_r, 3550.0),
        "copper": path(2.9, cop_r, 4.7), "usd": path(94.0, usd_r, 98.5),
    }
    closes["eurusd"] = 1.12 * (96.0 / closes["usd"]) ** 0.9
    ry_lvl = 0.6 + np.cumsum(rng.normal(0, 0.035, n) - 2.2 * f_pm)
    ry_lvl = 1.0 + (ry_lvl - ry_lvl.mean()) / ry_lvl.std() * 0.9
    closes["us10y"] = pd.Series(ry_lvl + 2.3, index=idx)

    daily = {}
    for k, c in closes.items():
        o = c.shift(1).fillna(c.iloc[0]) * (1 + rng.normal(0, 0.002, n))
        hi = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
        lo = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
        daily[k] = pd.DataFrame({"Open": o, "High": hi, "Low": lo, "Close": c,
                                 "Volume": rng.integers(20_000, 90_000, n).astype(float)})

    # päivänsisäinen 15 min data 60 päivältä: seuraa päiväkursseja + lyhytaikainen vire
    now = pd.Timestamp.now(tz="UTC").floor("15min")
    ts = pd.date_range(end=now, periods=60 * 24 * 4, freq="15min")
    ts = ts[ts.dayofweek < 5]
    m = len(ts)
    dts = closes["silver"].index[-70:]
    epoch = pd.Timestamp("1970-01-01", tz="UTC")   # sekunteina: toimii sekä pandas 2:ssa että 3:ssa
    xd = np.array([(pd.Timestamp(d).tz_localize("UTC") + pd.Timedelta(hours=21) - epoch).total_seconds()
                   for d in dts], float)
    xd[-1] = (now - epoch).total_seconds()
    yd = np.log(closes["silver"].iloc[-70:].to_numpy())
    base = np.interp((ts - epoch).total_seconds().to_numpy(float), xd, yd)
    e = np.zeros(m)
    shock = rng.normal(0, 0.0011, m)
    for i in range(1, m):   # hidas paluu + vauhti, jotta päivänsisäisellä vireellä on jotain mitattavaa
        e[i] = 0.985 * e[i - 1] + shock[i] + 0.25 * shock[i - 1]
    p = np.exp(base + e - e[-1])
    o = np.r_[p[0], p[:-1]]
    intraday = pd.DataFrame({
        "Open": o, "Close": p,
        "High": np.maximum(o, p) * (1 + np.abs(rng.normal(0, 0.0010, m))),
        "Low": np.minimum(o, p) * (1 - np.abs(rng.normal(0, 0.0010, m))),
        "Volume": rng.integers(200, 3000, m).astype(float)}, index=ts)

    weeks = pd.date_range(end=idx[-1] - pd.Timedelta(days=4), periods=520, freq="W-TUE")
    mom = closes["silver"].reindex(weeks, method="ffill").pct_change(8).fillna(0).values
    x = np.zeros(len(weeks))
    for i in range(1, len(weeks)):
        x[i] = 0.85 * x[i - 1] + 0.9 * mom[i] * 10 + rng.normal(0, 2.0)
    oi = rng.normal(150_000, 12_000, len(weeks))
    net = x / 100 * oi * 2.2 + 12_000
    cot = pd.DataFrame({"long": np.maximum(net, 0) + 30_000, "short": np.maximum(-net, 0) + 30_000,
                        "oi": oi}, index=weeks)
    cot["net"] = cot["long"] - cot["short"]
    cot["net_pct"] = cot["net"] / cot["oi"] * 100

    return MarketData(daily, intraday, pd.Series(ry_lvl, index=idx),
                      "USA 10 v reaalikorko (DEMO)", cot, True, [])


# ════════════════════════════════════════════════════════════════════════════
#  INDIKAATTORIT
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class Analysis:
    frame: pd.DataFrame            # päivätason hinnat ja apusarjat
    scores: pd.DataFrame           # päivätason pistemäärät (-1…+1) per indikaattori
    intraday: pd.DataFrame | None  # 15 min data + apusarjat
    intraday_score: float
    cot: pd.DataFrame | None


def analyse(md: MarketData) -> Analysis:
    s = md.daily["silver"]
    idx = s.index
    f = pd.DataFrame(index=idx)
    f["silver"], f["silver_high"], f["silver_low"] = s["Close"], s["High"], s["Low"]
    for k in ("gold", "copper", "usd", "eurusd"):
        f[k] = md.daily[k]["Close"].reindex(idx).ffill(limit=5) if k in md.daily else np.nan
    # Reaalikorko julkaistaan seuraavana päivänä -> viive 1 pv (ei kurkistusta tulevaan)
    f["real_yield"] = align_asof(md.real_yield, idx, 1) if md.real_yield is not None else np.nan

    sc = pd.DataFrame(index=idx)

    # 1. Kulta–hopea-suhde
    gsr = f["gold"] / f["silver"]
    f["gsr"] = gsr
    f["gsr_mean"] = gsr.rolling(756, min_periods=252).mean()
    f["gsr_std"] = gsr.rolling(756, min_periods=252).std()
    f["gsr_r20"] = gsr / gsr.shift(20) - 1
    z_lvl = (gsr - f["gsr_mean"]) / f["gsr_std"]
    f["gsr_r20_std"] = roll_std(f["gsr_r20"], 252)
    z_chg = f["gsr_r20"] / f["gsr_r20_std"]
    sc["gsr"] = np.tanh(0.5 * z_lvl - 0.5 * z_chg)

    # 2. Dollari, 4. kupari, 5. kulta – samalla trendikaavalla
    for k, sign in (("usd", -1), ("copper", 1), ("gold", 1)):
        z, ret, gap = trend_z(f[k])
        f[f"{k}_r20"], f[f"{k}_gap"], f[f"{k}_ema50"] = ret, gap, ema(f[k], 50)
        sc[k] = sign * np.tanh(z)

    # 3. Reaalikorko
    ry = f["real_yield"]
    f["ry_d20"] = ry - ry.shift(20)
    f["ry_mean"] = ry.rolling(252, min_periods=126).mean()
    z_d = f["ry_d20"] / roll_std(f["ry_d20"], 252)
    z_l = (ry - f["ry_mean"]) / ry.rolling(252, min_periods=126).std()
    sc["real_yield"] = -np.tanh(0.7 * z_d + 0.3 * z_l)

    # 6. COT – rahastojen nettoposition persentiili 3 vuoden ajalta, vastavirtaan
    cot = None
    if md.cot is not None and len(md.cot) > 60:
        cot = md.cot.copy()
        cot["pctile"] = cot["net_pct"].rolling(156, min_periods=52).apply(
            lambda w: (w[:-1] < w[-1]).mean() + 0.5 * (w[:-1] == w[-1]).mean(), raw=True)
        cot["score"] = -((2 * cot["pctile"] - 1) ** 3)
        # raportti koskee tiistaita, julkaistaan perjantaina -> viive 3 pv
        sc["cot"] = align_asof(cot["score"], idx, 3)
        f["cot_net_pct"] = align_asof(cot["net_pct"], idx, 3)
    else:
        sc["cot"] = np.nan

    # 7. Hopean oma trendi (lyhyempi aikaväli)
    z, ret, gap = trend_z(f["silver"], n_ret=10, n_ema=20)
    f["silver_r10"], f["silver_gap20"] = ret, gap
    f["silver_ema20"], f["silver_ema50"] = ema(f["silver"], 20), ema(f["silver"], 50)
    sc["trend"] = np.tanh(z)

    # 7b. Pitkä trendi (kuukausien mittakaava)
    z, ret, gap = trend_z(f["silver"], n_ret=60, n_ema=200, win=504)
    f["silver_r60"], f["silver_gap200"], f["silver_ema200"] = ret, gap, ema(f["silver"], 200)
    sc["long_trend"] = np.tanh(z)

    # 8. RSI
    r = rsi(f["silver"], 14)
    f["rsi"] = r
    rs = np.select([r > 70, r < 30], [-np.minimum(1, (r - 70) / 15), np.minimum(1, (30 - r) / 15)], 0.0)
    sc["rsi"] = pd.Series(rs, index=idx).where(r.notna())

    # Volatiliteetti (ATR) – ei äänestä, mutta näytetään päiväkauppiaalle
    tr = pd.concat([f["silver_high"] - f["silver_low"],
                    (f["silver_high"] - f["silver"].shift()).abs(),
                    (f["silver_low"] - f["silver"].shift()).abs()], axis=1).max(axis=1)
    f["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    # 9. Päivänsisäinen vire
    intra, intra_score = None, np.nan
    if md.intraday is not None and len(md.intraday) > 40:
        intra = md.intraday.copy()
        c, v = intra["Close"], intra["Volume"].fillna(0)
        bars_day = max(8, 1440 // bar_minutes())
        tp = (intra["High"] + intra["Low"] + c) / 3
        vsum = v.rolling(bars_day, min_periods=8).sum()
        vwap = (tp * v).rolling(bars_day, min_periods=8).sum() / vsum.replace(0, np.nan)
        intra["vwap"] = vwap.fillna(tp.rolling(bars_day, min_periods=8).mean())
        intra["ema9"], intra["ema21"] = ema(c, 9), ema(c, 21)
        a = intra["ema9"] / intra["ema21"] - 1
        b = c / intra["vwap"] - 1
        zwin = bars_day * 5   # z suhteessa viimeisten 5 päivän vaihteluun (ei kurkistusta tulevaan)
        sa = a.rolling(zwin, min_periods=bars_day).std()
        sb = b.rolling(zwin, min_periods=bars_day).std()
        intra["score"] = np.tanh(0.5 * a / sa + 0.5 * b / sb)
        sv = intra["score"].dropna()
        intra_score = float(sv.iloc[-1]) if len(sv) else np.nan

    return Analysis(f, sc, intra, intra_score, cot)


# ════════════════════════════════════════════════════════════════════════════
#  TODENNÄKÖISYYSKAAVA
# ════════════════════════════════════════════════════════════════════════════
#  Jokaisella indikaattorilla i on pistemäärä s_i ∈ [−1, +1] ja paino w_i.
#     nousu     = Σ w_i · max(s_i, 0)
#     lasku     = Σ w_i · max(−s_i, 0)
#     neutraali = Σ w_i · (1 − |s_i|)
#  Painot skaalataan summautumaan ykköseen saatavilla olevien indikaattorien kesken,
#  joten kolme lukua ovat aina yhteensä 100 %.

def combine(scores: dict, weights: dict) -> dict:
    """Yksi aikajänne: painot skaalataan saatavilla olevien mittarien kesken."""
    avail = {k: float(scores[k]) for k in weights
             if scores.get(k) is not None and math.isfinite(scores[k])}
    total_w = sum(weights.values())
    wsum = sum(weights[k] for k in avail)
    if wsum <= 0:
        return dict(bull=0.0, bear=0.0, neutral=1.0, parts={}, n=0, coverage=0.0)
    parts = {}
    bull = bear = neutral = 0.0
    for k, sv in avail.items():
        w = weights[k] / wsum
        pb, pr, pn = w * max(sv, 0), w * max(-sv, 0), w * (1 - abs(sv))
        parts[k] = dict(w=w, s=sv, bull=pb, bear=pr, neutral=pn)
        bull, bear, neutral = bull + pb, bear + pr, neutral + pn
    return dict(bull=bull, bear=bear, neutral=neutral, parts=parts, n=len(avail), coverage=wsum / total_w)


def combine_frame(S: pd.DataFrame, weights: dict, min_cov: float = 0.6):
    """Sama laskenta koko historialle. Rivi hylätään, jos alle 60 % painosta on saatavilla."""
    cols = [k for k in weights if k in S.columns]
    X = S[cols]
    W = pd.DataFrame({k: weights[k] for k in cols}, index=S.index).where(X.notna())
    tot = W.sum(axis=1)
    bull = (W * X.clip(lower=0)).sum(axis=1) / tot
    bear = (W * (-X).clip(lower=0)).sum(axis=1) / tot
    ok = tot >= min_cov * sum(weights.values())
    return bull.where(ok), bear.where(ok)


def verdict_of(bull: float, bear: float) -> tuple[str, str]:
    th = SIGNAL_THRESHOLD
    if bull >= th: return "Vahva noususignaali", "bull"
    if bear >= th: return "Vahva laskusignaali", "bear"
    if bull >= STRONG_THRESHOLD: return "Selvä nousukallistuma", "bull"
    if bear >= STRONG_THRESHOLD: return "Selvä laskukallistuma", "bear"
    if bull - bear >= 0.20: return "Lievä nousukallistuma", "bull"
    if bear - bull >= 0.20: return "Lievä laskukallistuma", "bear"
    return "Ei selvää suuntaa", "neutral"


# ── Kalibrointi ─────────────────────────────────────────────────────────────
#  Mittarien yksimielisyys ei ole todennäköisyys. Siksi nettolukema x = nousu − lasku
#  muunnetaan historiallisesti toteutuneeksi todennäköisyydeksi logistisella mallilla:
#       P(hinta korkeammalla) = 1 / (1 + e^−(a + b·x))
#  a ja b sovitetaan koko historiaan. Peräkkäiset havainnot menevät päällekkäin, joten
#  epävarmuus kerrotaan aikajänteen pituudella (varovainen korjaus).

def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def fit_logistic(x: np.ndarray, y: np.ndarray, iters: int = 60):
    X = np.column_stack([np.ones_like(x), x])
    beta, H = np.zeros(2), np.eye(2)
    for _ in range(iters):
        pr = _sigmoid(X @ beta)
        w = np.clip(pr * (1 - pr), 1e-9, None)
        H = X.T @ (X * w[:, None])
        step = np.linalg.solve(H + 1e-9 * np.eye(2), X.T @ (y - pr))
        beta = beta + step
        if np.max(np.abs(step)) < 1e-9:
            break
    return beta, np.linalg.inv(H)


def calibrate(net: pd.Series, fwd: pd.Series, h: int, x_now: float):
    d = pd.DataFrame({"x": net, "f": fwd}).dropna()
    d = d[d["f"] != 0]
    if len(d) < max(200, 20 * h) or not math.isfinite(x_now):
        return None, None
    x = d["x"].to_numpy(float)
    y = (d["f"] > 0).to_numpy(float)
    beta, cov = fit_logistic(x, y)
    cov = cov * h

    def pred(x0: float):
        v = np.array([1.0, x0])
        eta = float(v @ beta)
        se = math.sqrt(max(float(v @ cov @ v), 0.0))
        return float(_sigmoid(eta)), float(_sigmoid(eta - 1.645 * se)), float(_sigmoid(eta + 1.645 * se))

    p_now, lo, hi = pred(x_now)
    bz = float(beta[1] / math.sqrt(cov[1, 1]))
    rel = "pos" if bz >= 1.96 else "neg" if bz <= -1.96 else "none"
    info = dict(p=p_now, lo=lo, hi=hi, a=float(beta[0]), b=float(beta[1]), b_z=bz, relation=rel,
                base=float(y.mean()), n=int(len(d)), x_now=float(x_now),
                x_min=float(x.min()), x_max=float(x.max()),
                p_bull85=pred(SIGNAL_THRESHOLD)[0], p_bear85=pred(-SIGNAL_THRESHOLD)[0])
    return info, pred


def calib_text(c: dict, hz: dict) -> str:
    up = c["p"] >= 0.5
    pr = c["p"] if up else 1 - c["p"]
    lo, hi = (c["lo"], c["hi"]) if up else (1 - c["hi"], 1 - c["lo"])
    base = c["base"] if up else 1 - c["base"]
    word = "korkeammalla" if up else "matalammalla"
    t = (f"Vastaavalla lukemalla hopea on historiassa ollut {hz['ahead']} {word} {pct(pr, 0)}:ssa "
         f"kerroista (90 %:n luottamusväli {fi(lo * 100, 0)}–{fi(hi * 100, 0)} %). "
         f"Kaikkina havaintoina osuus on {pct(base, 0)}.")
    if c["relation"] == "none":
        t += (" Lukeman yhteys hinnan suuntaan ei ole tällä aikajänteellä tilastollisesti merkitsevä, "
              "joten käytä sitä vain taustatietona.")
    elif c["relation"] == "neg":
        t += " Huom: tällä aikajänteellä lukema on historiassa toiminut päinvastoin kuin pitäisi."
    return t


# ── Historiatesti ───────────────────────────────────────────────────────────

def backtest(bull: pd.Series, bear: pd.Series, price: pd.Series, hz: dict,
             cur_bull: float, cur_bear: float, since_label: str) -> dict:
    """Kuinka usein hopea oli aikajänteen päästä korkeammalla / matalammalla,
    kun malli näytti tiettyä lukemaa."""
    h = hz["forward"]
    fwd = price.shift(-h) / price - 1
    d = pd.DataFrame({"bull": bull, "bear": bear, "fwd": fwd}).dropna()
    if len(d) < max(200, 20 * h):
        return dict(ok=False)

    base_up = float((d["fwd"] > 0).mean())
    base_down = float((d["fwd"] < 0).mean())
    base_avg = float(d["fwd"].mean())
    MIN_N = 15 * h   # vähintään ~15 toisistaan riippumatonta jaksoa ennen johtopäätöstä
    ahead, unit = hz["ahead"], hz["unit"]

    def judge(side, n, up, down, avg):
        """Toimiko ennuste? Verrataan aina tavalliseen havaintoon, koska hopean
        pitkä nousutrendi nostaa kaikkien rivien keskiarvoa."""
        if side == "all":
            return "ref", "Vertailu"
        if n < MIN_N:
            return "few", "Liian vähän dataa"
        hit, base_hit = (up, base_up) if side == "bull" else (down, base_down)
        sgn = 1 if side == "bull" else -1
        if hit > 0.5 and sgn * avg > 0 and hit >= base_hit + 0.03:
            return "yes", "Toimi"
        if hit > base_hit and sgn * avg > sgn * base_avg:
            return "weak", "Heikosti"
        return "no", "Ei toiminut"

    def row(label, mask, side, key):
        sub = d[mask]
        n = int(len(sub))
        if n == 0:
            return dict(key=key, label=label, side=side, n=0, up=None, down=None, hit=None,
                        avg=None, verdict="none", verdict_label="Ei esiintynyt")
        up, down = float((sub["fwd"] > 0).mean()), float((sub["fwd"] < 0).mean())
        avg = float(sub["fwd"].mean())
        v, vl = judge(side, n, up, down, avg)
        if key == "mid":
            vl = "Ei ennustetta"
        return dict(key=key, label=label, side=side, n=n, up=up, down=down,
                    hit=down if side == "bear" else up, avg=avg, verdict=v, verdict_label=vl)

    def side_summary(side):
        mask = (d.bull >= 0.55) if side == "bull" else (d.bear >= 0.55)
        sub = d[mask]
        n = int(len(sub))
        word, Word = ("nousua", "Nousuennusteet") if side == "bull" else ("laskua", "Laskuennusteet")
        if n < MIN_N:
            return dict(side=side, verdict="few",
                        text=f"Malli on näyttänyt {word} vähintään 55 %:n lukemalla vain {n} "
                             f"{'havainnossa' if hz['key'] == 'day' else 'päivänä'}, mikä on liian vähän "
                             f"johtopäätöksiin (raja {MIN_N}, eli noin 15 toisistaan riippumatonta jaksoa).")
        up, down = float((sub["fwd"] > 0).mean()), float((sub["fwd"] < 0).mean())
        avg = float(sub["fwd"].mean())
        v, _ = judge(side, n, up, down, avg)
        t = (f"Kun malli ennusti {word} (lukema vähintään 55 %, {n} {unit}), hopean hinta oli "
             f"{ahead} noussut {pct(up, 0)}:ssa ja laskenut {pct(down, 0)}:ssa kerroista. "
             f"Keskimäärin hinta muuttui {pct(avg, 2, True)}. ")
        if side == "bull":
            t += {"yes": f"{Word} ovat toteutuneet tavallista useammin.",
                  "weak": "Tulos on hieman tavallista parempi, mutta ero on pieni.",
                  "no": f"{Word} eivät ole toteutuneet tavallista paremmin."}[v]
        else:
            t += {"yes": f"{Word} ovat toteutuneet: hinta laski useammin kuin nousi.",
                  "weak": "Hinta nousi tavallista heikommin, mutta ei varsinaisesti laskenut. "
                          "Laskuennuste on toiminut siis lähinnä varoituksena, ei myyntisignaalina.",
                  "no": (f"{Word} eivät ole toteutuneet: hinta päinvastoin nousi keskimäärin."
                         if avg > 0 else f"{Word} eivät ole toimineet tavallista paremmin.")}[v]
        return dict(side=side, verdict=v, text=t)

    th, st = SIGNAL_THRESHOLD, STRONG_THRESHOLD
    rows = [
        row(f"Nousu ≥ {th*100:.0f} %", d.bull >= th, "bull", "b85"),
        row(f"Nousu {st*100:.0f}–{th*100:.0f} %", (d.bull >= st) & (d.bull < th), "bull", "b70"),
        row(f"Nousu 55–{st*100:.0f} %", (d.bull >= 0.55) & (d.bull < st), "bull", "b55"),
        row("Ei selvää kallistumaa", (d.bull < 0.55) & (d.bear < 0.55), "all", "mid"),
        row(f"Lasku 55–{st*100:.0f} %", (d.bear >= 0.55) & (d.bear < st), "bear", "r55"),
        row(f"Lasku {st*100:.0f}–{th*100:.0f} %", (d.bear >= st) & (d.bear < th), "bear", "r70"),
        row(f"Lasku ≥ {th*100:.0f} %", d.bear >= th, "bear", "r85"),
        row("Kaikki havainnot (vertailu)", d.bull.notna(), "all", "all"),
    ]
    cb, cr = cur_bull, cur_bear
    cur = ("b85" if cb >= th else "r85" if cr >= th else "b70" if cb >= st else "r70" if cr >= st
           else "b55" if cb >= 0.55 else "r55" if cr >= 0.55 else "mid")
    return dict(
        ok=True, rows=rows, current=cur, since=since_label, n=int(len(d)), unit=unit,
        base_up=base_up, base_down=base_down, base_avg=base_avg, min_n=MIN_N,
        summary=[side_summary("bull"), side_summary("bear")],
        base_text=(f"Vertailuksi kaikki havainnot: hopea oli {ahead} noussut "
                   f"{pct(base_up, 0)}:ssa kerroista ja keskimäärin {pct(base_avg, 2, True)}."
                   + (" Koska hopea on tällä jaksolla noussut, keskimuutos on usein plussalla myös "
                      "laskulukemilla. Ratkaisevaa on, jäikö tulos tavallista heikommaksi."
                      if base_avg > 0 else "")),
        max_bull=float(d["bull"].max()), max_bear=float(d["bear"].max()),
        share_signal=float(((d.bull >= th) | (d.bear >= th)).mean()),
    )


# ── Aikajänteiden laskenta ─────────────────────────────────────────────────

def day_frame(an: "Analysis") -> pd.DataFrame | None:
    """Päiväkaupan pistemäärät jokaiselle 15 min kynttilälle. Trendi ja RSI otetaan
    edellisen kaupankäyntipäivän päätöksestä, jottei historiatesti näe tulevaa."""
    it = an.intraday
    if it is None or "score" not in it:
        return None
    td = trade_dates(it.index)
    u = pd.DatetimeIndex(sorted(set(td)))
    base = an.scores[["trend", "rsi"]]
    pos = base.index.searchsorted(u, side="left") - 1
    vals = base.iloc[np.clip(pos, 0, None)].to_numpy(float)
    vals[pos < 0] = np.nan
    m = pd.DataFrame(vals, index=u, columns=base.columns).reindex(td)
    return pd.DataFrame({"intraday": it["score"].to_numpy(), "trend": m["trend"].to_numpy(),
                         "rsi": m["rsi"].to_numpy()}, index=it.index)


def evaluate_horizons(an: "Analysis", cur_daily: dict, tz: ZoneInfo) -> dict:
    HZ["day"]["forward"] = max(1, round(DAY_AHEAD_MINUTES / bar_minutes()))
    out = {}
    for hz in HORIZONS:
        key, w = hz["key"], hz["weights"]
        if key == "day":
            S = day_frame(an)
            price = an.intraday["Close"] if an.intraday is not None else None
        else:
            S = an.scores[[k for k in w if k in an.scores.columns]]
            price = an.frame["silver"]
        base = dict(key=key, name=hz["name"], span=hz["span"], ahead=hz["ahead"],
                    ahead_short=hz["ahead_short"], forward=hz["forward"], unit=hz["unit"],
                    note=hz["note"], weights=w)
        if S is None or price is None or not len(S):
            out[key] = dict(base, available=False)
            continue
        cur = S.iloc[-1].to_dict() if key == "day" else {k: cur_daily.get(k) for k in w}
        comb = combine(cur, w)
        if comb["n"] == 0:
            out[key] = dict(base, available=False)
            continue
        bull_s, bear_s = combine_frame(S, w)
        fwd = price.shift(-hz["forward"]) / price - 1
        x_now = comb["bull"] - comb["bear"]
        cal, pred = calibrate(bull_s - bear_s, fwd, hz["forward"], x_now)
        first = bull_s.dropna().index
        since = ((pv(first[0].tz_convert(tz)) if key == "day" else f"{first[0].month}/{first[0].year}")
                 if len(first) else "")
        bt = backtest(bull_s, bear_s, price, hz, comb["bull"], comb["bear"], since)
        label, tone = verdict_of(comb["bull"], comb["bear"])

        contrib = sorted(((k, p["w"] * p["s"]) for k, p in comb["parts"].items()), key=lambda x: x[1])
        pos_ = [IND_BY_KEY[k]["pos"] for k, c in reversed(contrib) if c > 0.02][:2]
        neg_ = [IND_BY_KEY[k]["neg"] for k, c in contrib if c < -0.02][:2]
        summ = []
        if pos_: summ.append(f"Nousun puolella: {join_fi(pos_)}.")
        if neg_: summ.append(f"Laskun puolella: {join_fi(neg_)}.")
        if not summ: summ.append("Mikään mittari ei anna selvää suuntaa.")
        big = max(comb["bull"], comb["bear"])
        out[key] = dict(base, available=True, bull=comb["bull"], bear=comb["bear"], neutral=comb["neutral"],
                        net=x_now, n=comb["n"], coverage=comb["coverage"], parts=comb["parts"],
                        label=label, tone=tone, signal=big >= SIGNAL_THRESHOLD,
                        to_threshold=max(0.0, SIGNAL_THRESHOLD - big), summary=" ".join(summ),
                        calib=cal, calib_text=calib_text(cal, hz) if cal else "",
                        backtest=bt, _series=(bull_s, bear_s), _pred=pred, _S=S)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  SELKOKIELISET TEKSTIT JA SIVUN DATA
# ════════════════════════════════════════════════════════════════════════════

def describe(an: Analysis, md: MarketData, tz: ZoneInfo) -> dict[str, dict]:
    """Jokaiselle indikaattorille: arvo, lisätieto, nykytilan selitys ja minikaavio."""
    f = an.frame
    L = lambda col, age=7: last_valid(f[col], age) if col in f else np.nan  # noqa: E731
    y1 = f.index[-1] - pd.Timedelta(days=365)
    m6 = f.index[-1] - pd.Timedelta(days=183)
    out: dict[str, dict] = {}

    def line_chart(cols, since, unit="", hlines=None, names=None, nd=4):
        sub = f.loc[f.index >= since]
        return dict(kind="line", t=dates(sub.index),
                    series=[dict(name=(names or cols)[i], y=to_list(sub[c], nd), main=i == 0)
                            for i, c in enumerate(cols)],
                    unit=unit, hlines=hlines or [])

    # Kulta–hopea-suhde
    g, gm, gs, g20 = L("gsr"), L("gsr_mean"), L("gsr_std"), L("gsr_r20")
    if math.isfinite(g):
        rel = "korkeampi" if g > gm else "matalampi"
        txt = (f"Suhde on nyt {fi(g)}, mikä on {rel} kuin kolmen vuoden keskiarvo {fi(gm)} "
               f"(tavallinen vaihteluväli {fi(gm - gs, 0)}–{fi(gm + gs, 0)}). ")
        if math.isfinite(g20):
            txt += (f"Kuukaudessa suhde on {'laskenut' if g20 < 0 else 'noussut'} {pct(abs(g20))}, eli hopea on "
                    f"{'pärjännyt kultaa paremmin' if g20 < 0 else 'jäänyt kullasta jälkeen'}.")
        z = gsr_zone(g)
        if z:
            txt += (f" Pitkän historian mittakaavassa suhde on alueella {z['range']}: "
                    f"{z['title'][0].lower() + z['title'][1:]}.")
        out["gsr"] = dict(value=fi(g), sub=f"20 pv: {pct(g20, 1, True)}", now=txt,
                          chart=line_chart(["gsr", "gsr_mean"], y1, "", names=["Suhde", "3 v keskiarvo"]))

    # Dollari
    u = L("usd")
    if math.isfinite(u):
        r20, gap = L("usd_r20"), L("usd_gap")
        txt = (f"Dollari-indeksi on {fi(u, 2)}: kuukaudessa {pct(r20, 1, True)} ja "
               f"{pct(abs(gap))} 50 päivän keskiarvonsa {'yläpuolella' if gap > 0 else 'alapuolella'}. "
               f"{'Vahvistuva' if r20 > 0 else 'Heikkenevä'} dollari "
               f"{'jarruttaa' if r20 > 0 else 'tukee'} hopean hintaa.")
        out["usd"] = dict(value=fi(u, 2), sub=f"20 pv: {pct(r20, 1, True)}", now=txt,
                          chart=line_chart(["usd", "usd_ema50"], y1, "", names=["DXY", "50 pv ka."]))

    # Reaalikorko
    ry = L("real_yield", 10)
    if math.isfinite(ry):
        d20, rm = L("ry_d20", 10), L("ry_mean", 10)
        last_date = md.real_yield.dropna().index[-1] if md.real_yield is not None else None
        txt = (f"Reaalikorko on {fi(ry, 2)} %. Kuukaudessa se on {'noussut' if d20 > 0 else 'laskenut'} "
               f"{fi(abs(d20) * 100, 0)} korkopistettä (0,01 %-yks). Vuoden keskiarvo on {fi(rm, 2)} %. ")
        txt += ("Nousevat korot tekevät korottomasta hopeasta vähemmän houkuttelevan."
                if d20 > 0 else "Laskevat korot keventävät hopean pitämisen kustannusta.")
        if "nimellis" in md.real_yield_label:
            txt += " (Huom. FRED ei vastannut – käytössä nimelliskorko.)"
        out["real_yield"] = dict(
            value=f"{fi(ry, 2)} %", sub=f"20 pv: {fi(d20 * 100, 0, True)} kp",
            now=txt, asof=pv(last_date) if last_date is not None else "",
            chart=line_chart(["real_yield", "ry_mean"], y1, " %", names=["Reaalikorko", "1 v ka."]))

    # Kupari & kulta
    for k, unit, nd, label in (("copper", "$/lb", 2, "Kupari"), ("gold", "$/oz", 0, "Kulta")):
        v = L(k)
        if math.isfinite(v):
            r20, gap = L(f"{k}_r20"), L(f"{k}_gap")
            txt = (f"{label} {fi(v, nd)} {unit}: kuukaudessa {pct(r20, 1, True)}, ja hinta on "
                   f"{pct(abs(gap))} 50 päivän keskiarvon {'yläpuolella' if gap > 0 else 'alapuolella'}.")
            if k == "copper":
                txt += (" Teollisuuden kysyntä vaikuttaa vahvistuvan." if r20 > 0
                        else " Teollisuuden kysyntä näyttää hiipuvan.")
            else:
                txt += (" Jalometallien yleisvire on myönteinen." if r20 > 0
                        else " Jalometallien yleisvire on vaisu.")
            out[k] = dict(value=f"{fi(v, nd)} {unit}", sub=f"20 pv: {pct(r20, 1, True)}", now=txt,
                          chart=line_chart([k, f"{k}_ema50"], y1, "", names=[label, "50 pv ka."]))

    # COT
    if an.cot is not None and an.cot["pctile"].notna().any():
        c = an.cot.dropna(subset=["pctile"])
        last = c.iloc[-1]
        p = float(last["pctile"])
        mood = ("äärimmäisen nousuhenkisiä" if p > 0.9 else "nousuhenkisiä" if p > 0.65 else
                "äärimmäisen varovaisia" if p < 0.1 else "varovaisia" if p < 0.35 else "keskitasolla")
        txt = (f"Rahastot ovat netto{'ostossa' if last['net'] > 0 else 'myynnissä'} "
               f"{fi(abs(last['net']), 0)} sopimusta ({fi(last['net_pct'], 1)} % avoimista sopimuksista). "
               f"Positio on suurempi kuin {fi(p * 100, 0)} %:ssa viimeisten 3 vuoden viikoista – rahastot ovat {mood}. ")
        txt += ("Kun ostajat ovat jo markkinassa, laskuriski kasvaa." if p > 0.75 else
                "Ostovaraa on paljon, mikä voi ruokkia nousua." if p < 0.25 else
                "Vaikutus on pieni, koska positio ei ole ääripäässä.")
        sub = c.loc[c.index >= c.index[-1] - pd.Timedelta(days=3 * 365)]
        out["cot"] = dict(
            value=f"{fi(last['net_pct'], 1)} %", sub=f"persentiili {fi(p * 100, 0)}",
            now=txt, asof=pv(c.index[-1]),
            chart=dict(kind="bar", t=dates(sub.index),
                       series=[dict(name="Nettopositio % OI", y=to_list(sub["net_pct"], 2), main=True)],
                       unit=" %", hlines=[dict(y=0, label="")]))

    # Hopean trendi
    px = L("silver")
    r10, gap20 = L("silver_r10"), L("silver_gap20")
    e20, e50 = L("silver_ema20"), L("silver_ema50")
    txt = (f"Hopea on muuttunut kahdessa viikossa {pct(r10, 1, True)} ja on "
           f"{pct(abs(gap20))} 20 päivän keskiarvonsa {'yläpuolella' if gap20 > 0 else 'alapuolella'}. "
           + ("Myös pidempi trendi on ylöspäin (20 pv keskiarvo on 50 pv keskiarvon yläpuolella)."
              if e20 > e50 and gap20 > 0 else
              "Pidempi trendi on silti ylöspäin (20 pv keskiarvo on 50 pv keskiarvon yläpuolella), "
              "joten kyse voi olla notkahduksesta nousun sisällä." if e20 > e50 else
              "Myös pidempi trendi on alaspäin (20 pv keskiarvo on 50 pv keskiarvon alapuolella)."
              if gap20 < 0 else
              "Pidempi trendi on silti alaspäin (20 pv keskiarvo on 50 pv keskiarvon alapuolella), "
              "joten kyse voi olla lyhyestä pomppusta laskun sisällä."))
    out["trend"] = dict(value=f"{pct(r10, 1, True)}", sub="10 pv muutos", now=txt,
                        chart=line_chart(["silver", "silver_ema20", "silver_ema50"], m6, " $",
                                         names=["Hopea", "20 pv ka.", "50 pv ka."]))

    # Pitkä trendi
    r60, g200 = L("silver_r60"), L("silver_gap200")
    if math.isfinite(r60) and math.isfinite(g200):
        y2 = f.index[-1] - pd.Timedelta(days=730)
        txt = (f"Hopea on muuttunut kolmessa kuukaudessa {pct(r60, 1, True)} ja on {pct(abs(g200))} "
               f"200 päivän keskiarvonsa {'yläpuolella' if g200 > 0 else 'alapuolella'}. ")
        txt += ("Pitkä nousutrendi on voimassa." if g200 > 0 and r60 > 0 else
                "Pitkä laskutrendi on voimassa." if g200 < 0 and r60 < 0 else
                "Suunta on epäselvä: kolmen kuukauden muutos ja keskiarvon taso ovat ristiriidassa.")
        out["long_trend"] = dict(value=pct(r60, 1, True), sub="60 pv muutos", now=txt,
                                 chart=line_chart(["silver", "silver_ema200"], y2, " $",
                                                  names=["Hopea", "200 pv ka."]))

    # RSI
    rv = L("rsi")
    zone = ("ylikuumentunut – nousu on ollut poikkeuksellisen nopea" if rv > 70 else
            "ylimyyty – lasku on ollut poikkeuksellisen nopea" if rv < 30 else
            "normaalialueella (30–70), joten mittari ei anna signaalia")
    out["rsi"] = dict(value=fi(rv, 0), sub="RSI 14 pv", now=f"RSI on {fi(rv, 0)}: {zone}.",
                      chart=line_chart(["rsi"], m6, "", names=["RSI"],
                                       hlines=[dict(y=70, label="70"), dict(y=30, label="30")]))

    # Päivänsisäinen
    if an.intraday is not None:
        it = an.intraday.dropna(subset=["vwap"])
        lc = it.iloc[-1]
        gv = lc["Close"] / lc["vwap"] - 1
        tloc = pvk(it.index[-1].tz_convert(tz))
        txt = (f"Hinta on {pct(abs(gv), 2)} vuorokauden keskihinnan (VWAP) "
               f"{'yläpuolella' if gv > 0 else 'alapuolella'}, ja nopea keskiarvo on hitaan "
               f"{'yläpuolella – lyhyt vire on ylöspäin' if lc['ema9'] > lc['ema21'] else 'alapuolella – lyhyt vire on alaspäin'}. "
               f"Viimeisin kynttilä {tloc}.")
        sub_it = it.iloc[-96:]
        out["intraday"] = dict(
            value=f"{pct(gv, 2, True)}", sub="vs. VWAP", now=txt,
            chart=dict(kind="line", t=local_times(sub_it.index, tz),
                       series=[dict(name="Hopea", y=to_list(sub_it["Close"], 3), main=True),
                               dict(name="VWAP", y=to_list(sub_it["vwap"], 3), main=False)],
                       unit=" $", hlines=[]))
    return out


def gsr_example(f: pd.DataFrame) -> dict | None:
    """Kulta–hopea-suhteen pistemäärä laskettuna auki tämän päivän luvuilla."""
    L = lambda c: last_valid(f[c])  # noqa: E731
    gold, silver, ratio = L("gold"), L("silver"), L("gsr")
    mean, std, r20, r20s = L("gsr_mean"), L("gsr_std"), L("gsr_r20"), L("gsr_r20_std")
    if not all(math.isfinite(x) for x in (gold, silver, ratio, mean, std, r20, r20s)) or std <= 0 or r20s <= 0:
        return None
    zl, zc = (ratio - mean) / std, r20 / r20s
    inner = 0.5 * zl - 0.5 * zc
    sc = round(math.tanh(inner), 3)   # sama pyöristys kuin indikaattorikortilla
    eff, _ = effect_of(sc)
    steps = [
        f"<b>Suhde</b> = kullan hinta / hopean hinta = {fi(gold, 0)} $ / {fi(silver, 2)} $ = <b>{fi(ratio)}</b>. "
        f"Yhdellä kultaunssilla saa siis {fi(ratio)} unssia hopeaa.",
        f"<b>Taso</b>: kolmen vuoden keskiarvo on {fi(mean)} ja tavallinen vaihtelu (keskihajonta) {fi(std)}. "
        f"Suhde on ({fi(ratio)} − {fi(mean)}) / {fi(std)} ≈ <b>{fi(zl, 2, True)}</b> keskihajontaa keskiarvon "
        f"{'yläpuolella' if zl >= 0 else 'alapuolella'}. "
        + ("Plus tarkoittaa, että hopea on tavallista halvempaa." if zl >= 0
           else "Miinus tarkoittaa, että hopea on tavallista kalliimpaa."),
        f"<b>Suunta</b>: 20 päivässä suhde on muuttunut {pct(r20, 1, True)}, kun tavallinen 20 päivän heilahdus on "
        f"± {pct(r20s, 1)}. Muutos on siis {pct(r20, 1, True)} / {pct(r20s, 1)} ≈ <b>{fi(zc, 2, True)}</b> keskihajontaa. "
        + ("Laskeva suhde tarkoittaa, että hopea on jo alkanut kiriä kultaa kiinni." if zc < 0
           else "Nouseva suhde tarkoittaa, että hopea jää yhä jälkeen kullasta."),
        f"<b>Pisteet</b>: s = tanh(0,5 × ({fi(zl, 2, True)}) − 0,5 × ({fi(zc, 2, True)})) ≈ tanh({fi(inner, 2, True)}) "
        f"≈ <b>{fi(sc, 2, True)}</b> → {eff.lower()}. Tanh-funktio vain puristaa luvun välille −1…+1.",
    ]
    at_mean, at_60 = gold / mean, gold / 60
    whatif = (f"<b>Mitä tämä tarkoittaa hinnalle?</b> Jos kulta pysyisi {fi(gold, 0)} $:ssa ja suhde palaisi kolmen "
              f"vuoden keskiarvoon {fi(mean)}, hopea maksaisi {fi(gold, 0)} / {fi(mean)} = <b>{fi(at_mean, 2)} $</b> "
              f"({pct(at_mean / silver - 1, 1, True)}). Pitkän aikavälin keskitasolla 60 hopea maksaisi "
              f"<b>{fi(at_60, 2)} $</b> ({pct(at_60 / silver - 1, 1, True)}). Tämä on laskuesimerkki suhteen logiikasta, "
              f"ei hintaennuste: suhde voi palata myös niin, että kulta halpenee.")
    z = gsr_zone(ratio)
    return dict(steps=steps, whatif=whatif, ratio=fi(ratio), zone=z["key"] if z else None,
                zone_text=f"{z['range']} · {z['title']}" if z else "")


def current_daily_scores(an: Analysis) -> dict:
    cur = {k: last_valid(an.scores[k], 10 if k in ("cot", "real_yield") else 7) for k in an.scores.columns}
    if an.cot is not None and an.cot["score"].notna().any():
        cur["cot"] = float(an.cot["score"].dropna().iloc[-1])   # julkaistu raportti = käytettävissä
    return cur


def build_payload(md: MarketData, an: Analysis, refresh: int, log_dir: Path | None = None,
                  write_log: bool = True, out_dir: Path | None = None) -> dict:
    tz = ZoneInfo(TIMEZONE)
    f = an.frame
    now_utc = datetime.now(timezone.utc)

    cur = current_daily_scores(an)
    hres = evaluate_horizons(an, cur, tz)
    day_S = hres["day"].get("_S") if hres["day"].get("available") else None
    cur_all = dict(cur)
    cur_all["intraday"] = an.intraday_score

    # Hinta
    if an.intraday is not None:
        price = float(an.intraday["Close"].iloc[-1])
        price_time = an.intraday.index[-1].tz_convert(tz)
    else:
        price = float(f["silver"].iloc[-1])
        price_time = None
    prev_close = float(f["silver"].iloc[-2])
    if price_time is not None:
        if f.index[-1] < trade_dates(an.intraday.index[-1:])[0]:
            prev_close = float(f["silver"].iloc[-1])
    chg = price / prev_close - 1
    eurusd = last_valid(f["eurusd"]) if "eurusd" in f else np.nan
    eur = price / eurusd if math.isfinite(eurusd) else None

    atr = last_valid(f["atr"])
    atr_pct = atr / f["silver"].iloc[-1]
    atr_hist = (f["atr"] / f["silver"]).iloc[-252:]
    atr_p = float((atr_hist < atr_pct).mean())
    vol_label = "rauhallinen" if atr_p < 0.3 else "korkea" if atr_p > 0.7 else "normaali"

    # Indikaattorikortit
    desc = describe(an, md, tz)
    cards = []
    for d in INDICATORS:
        k = d["key"]
        sv = cur_all.get(k, np.nan)
        sv = np.nan if sv is None else sv
        eff, etone = effect_of(sv)
        info = desc.get(k, {})
        cards.append(dict(
            key=k, name=d["name"], group=d["group"],
            weights=[dict(h=h["key"], name=h["name"], w=h["weights"][k]) for h in HORIZONS if k in h["weights"]],
            score=None if not math.isfinite(sv) else round(float(sv), 3),
            available=math.isfinite(sv),
            effect=eff, tone=etone, what=d["what"], formula=d["formula"],
            value=info.get("value", "–"), sub=info.get("sub", ""),
            now=info.get("now", "Dataa ei saatu haettua tällä ajokerralla."),
            asof=info.get("asof", ""), chart=info.get("chart"),
        ))

    # Signaaliloki ja seuranta
    tracking = dict(enabled=False)
    if log_dir is not None:
        try:
            tracking = update_logs(Path(log_dir), an, hres, now_utc, write=write_log and not md.demo)
            if out_dir is not None:
                copy_logs(Path(log_dir), Path(out_dir))
        except Exception as e:  # noqa: BLE001
            log.warning("Signaaliloki epäonnistui: %s", e)
            tracking = dict(enabled=False, error=str(e))

    # Isot kaaviot
    charts = {}
    if an.intraday is not None:
        it = an.intraday.iloc[-int(1440 / bar_minutes() * 2.2):]
        charts["intraday"] = dict(t=local_times(it.index, tz),
                                  o=to_list(it["Open"], 3), h=to_list(it["High"], 3),
                                  l=to_list(it["Low"], 3), c=to_list(it["Close"], 3),
                                  ema9=to_list(it["ema9"], 3), ema21=to_list(it["ema21"], 3),
                                  vwap=to_list(it["vwap"], 3))
    d2 = f.loc[f.index >= f.index[-1] - pd.Timedelta(days=730)]
    daily_charts = {}
    for key in ("swing", "long"):
        hr = hres[key]
        if hr.get("available"):
            sb, sr = hr["_series"]
            daily_charts[key] = dict(bull=to_list(sb.reindex(d2.index) * 100, 1),
                                     bear=to_list(sr.reindex(d2.index) * 100, 1))
    charts["daily"] = dict(t=dates(d2.index), c=to_list(d2["silver"], 3),
                           ema20=to_list(d2["silver_ema20"], 3), ema50=to_list(d2["silver_ema50"], 3),
                           signals=daily_charts)
    gall = f.dropna(subset=["gsr"])
    charts["gsr"] = dict(t=dates(gall.index), v=to_list(gall["gsr"], 2), mean=to_list(gall["gsr_mean"], 2),
                         zones=GSR_ZONES, example=gsr_example(f))

    # Datan tuoreus
    fresh = [dict(name="Hopea, kulta, kupari, dollari", src="Yahoo Finance",
                  url="https://finance.yahoo.com/quote/SI%3DF/",
                  when=(pvk(price_time) if price_time is not None else pv(f.index[-1])),
                  note="futuurihinnat voivat olla n. 10–15 min viiveellä")]
    if md.real_yield is not None:
        fresh.append(dict(name=md.real_yield_label, src="FRED",
                          url="https://fred.stlouisfed.org/series/DFII10",
                          when=pv(md.real_yield.dropna().index[-1]),
                          note="päivittyy kerran päivässä"))
    if an.cot is not None:
        fresh.append(dict(name="Rahastojen positiot (Managed Money)", src="CFTC COT",
                          url="https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm",
                          when=pv(an.cot.index[-1]),
                          note="tilanne tiistailta, julkaistaan perjantaisin"))

    horizons = []
    for h in HORIZONS:
        hr = {k: v for k, v in hres[h["key"]].items() if not k.startswith("_")}
        if hr.get("parts"):   # sama pyöristys kuin indikaattorikorteilla (3 desimaalia)
            hr["parts"] = {k: dict(v, s=round(v["s"], 3)) for k, v in hr["parts"].items()}
        horizons.append(hr)
    return dict(
        meta=dict(generated=pvvk(now_utc.astimezone(tz)),
                  generated_iso=now_utc.isoformat(), demo=md.demo, refresh=refresh,
                  threshold=SIGNAL_THRESHOLD, strong=STRONG_THRESHOLD, call_threshold=CALL_THRESHOLD,
                  errors=md.errors, fresh=fresh, timezone=TIMEZONE, main=MAIN_HORIZON),
        price=dict(usd=fi(price, 2), eur=fi(eur, 2) if eur else None,
                   eur_kg=fi(eur / TROY_OUNCE_G * 1000, 0) if eur else None,
                   chg=pct(chg, 2, True), chg_abs=fi(price - prev_close, 2, True),
                   up=chg >= 0,
                   time=pvk(price_time) if price_time is not None else "",
                   atr=fi(atr, 2), atr_pct=pct(atr_pct, 1), vol_label=vol_label),
        horizons=horizons,
        indicators=cards,
        charts=charts,
        tracking=tracking,
    )


# ════════════════════════════════════════════════════════════════════════════
#  SIGNAALILOKI
# ════════════════════════════════════════════════════════════════════════════
#  Kaksi Excel-yhteensopivaa CSV-tiedostoa (erotin ; ja desimaalipilkku):
#    signaaliloki_paiva.csv  – yksi rivi kaupankäyntipäivää kohden päätöskurssin jälkeen
#                              (lyhyt ja pitkä aikajänne + kaikkien mittarien pisteet)
#    signaaliloki_tunti.csv  – yksi rivi tunnissa markkinan ollessa auki (päiväkauppa)
#  Rivit lisätään vain, eikä vanhoja ennusteita muuteta. Ainoastaan toteutuneen
#  muutoksen sarakkeet täytetään jälkikäteen, kun aikajänne on kulunut.

CSV_KW = dict(sep=";", decimal=",", encoding="utf-8-sig")


def read_log(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    return pd.read_csv(path, dtype={"pvm": str, "tunti_utc": str, "kynttila_utc": str,
                                    "kirjattu_utc": str}, **CSV_KW)


def write_log(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False, float_format="%.6g", **CSV_KW)
    tmp.replace(path)


def _hz_cols(row: dict, key: str, hr: dict, bull=None, bear=None, p=None) -> None:
    pref = CSV_PREFIX[key]
    if bull is None:
        bull, bear = hr.get("bull"), hr.get("bear")
        p = (hr.get("calib") or {}).get("p")
    row[f"{pref}_nousu"] = None if bull is None else round(float(bull), 4)
    row[f"{pref}_lasku"] = None if bear is None else round(float(bear), 4)
    row[f"{pref}_neutraali"] = None if bull is None else round(1 - float(bull) - float(bear), 4)
    row[f"{pref}_p_nousu"] = None if p is None else round(float(p), 4)


def update_logs(log_dir: Path, an: Analysis, hres: dict, now_utc: datetime, write: bool = True) -> dict:
    f = an.frame
    dpath, hpath = log_dir / LOG_DAILY_FILE, log_dir / LOG_HOURLY_FILE
    dlog, hlog = read_log(dpath), read_log(hpath)
    stamp = now_utc.strftime("%Y-%m-%d %H:%M")
    added = []

    # 1) Päivärivi: viimeisin päättynyt kaupankäyntipäivä (päätös klo 17 NY-aikaa)
    ny = now_utc.astimezone(NY)
    D = pd.Timestamp(ny.date())
    if ny.hour < 17:
        D -= pd.Timedelta(days=1)
    while D.weekday() >= 5:
        D -= pd.Timedelta(days=1)
    dkey = D.strftime("%Y-%m-%d")
    have = set(dlog["pvm"]) if "pvm" in dlog else set()
    if write and D in f.index and dkey not in have:
        row = dict(pvm=dkey, kirjattu_utc=stamp, hopea_usd=round(float(f.at[D, "silver"]), 4),
                   eurusd=None if not math.isfinite(f.at[D, "eurusd"]) else round(float(f.at[D, "eurusd"]), 5))
        for key in ("swing", "long"):
            hr = hres[key]
            if hr.get("available"):
                sb, sr = hr["_series"]
                b, r = sb.get(D), sr.get(D)
                p = hr["_pred"](float(b - r))[0] if hr.get("_pred") and b is not None and math.isfinite(b) else None
                _hz_cols(row, key, hr, b, r, p)
        for k in an.scores.columns:
            v = an.scores.at[D, k]
            row[f"s_{k}"] = None if not math.isfinite(v) else round(float(v), 4)
        row["muutos_5pv"], row["muutos_40pv"] = None, None
        dlog = pd.concat([dlog, pd.DataFrame([row])], ignore_index=True)
        added.append(f"päivä {dkey}")

    # 2) Tuntirivi: markkina auki (viimeisin kynttilä enintään 30 min vanha)
    hr = hres["day"]
    if write and an.intraday is not None and hr.get("available"):
        last_t = an.intraday.index[-1]
        if (pd.Timestamp(now_utc) - last_t) <= pd.Timedelta(minutes=30):
            hkey = last_t.floor("h").strftime("%Y-%m-%d %H:00")
            have = set(hlog["tunti_utc"]) if "tunti_utc" in hlog else set()
            if hkey not in have:
                row = dict(tunti_utc=hkey, kynttila_utc=last_t.strftime("%Y-%m-%d %H:%M"), kirjattu_utc=stamp,
                           hopea_usd=round(float(an.intraday["Close"].iloc[-1]), 4))
                _hz_cols(row, "day", hr)
                _hz_cols(row, "swing", hres["swing"]) if hres["swing"].get("available") else None
                iv = an.intraday_score
                row["s_intraday"] = None if not math.isfinite(iv) else round(float(iv), 4)
                row["muutos_4h"] = None
                hlog = pd.concat([hlog, pd.DataFrame([row])], ignore_index=True)
                added.append(f"tunti {hkey}")

    # 3) Täytä toteutuneet muutokset
    if len(dlog):
        last_ok = len(f) - 2   # viimeinen rivi voi olla kesken oleva päivä
        for col, h in (("muutos_5pv", HZ["swing"]["forward"]), ("muutos_40pv", HZ["long"]["forward"])):
            if col not in dlog:
                dlog[col] = np.nan
            for i in dlog.index[dlog[col].isna()]:
                pos = f.index.get_indexer([pd.Timestamp(dlog.at[i, "pvm"])])[0]
                if pos >= 0 and pos + h <= last_ok:
                    dlog.at[i, col] = round(float(f["silver"].iloc[pos + h] / dlog.at[i, "hopea_usd"] - 1), 6)
    if len(hlog) and an.intraday is not None:
        h = HZ["day"]["forward"]
        it = an.intraday
        if "muutos_4h" not in hlog:
            hlog["muutos_4h"] = np.nan
        times = pd.to_datetime(hlog["kynttila_utc"], utc=True)
        for i in hlog.index[hlog["muutos_4h"].isna()]:
            pos = it.index.get_indexer([times[i]])[0]
            if pos >= 0 and pos + h <= len(it) - 1:
                hlog.at[i, "muutos_4h"] = round(float(it["Close"].iloc[pos + h] / hlog.at[i, "hopea_usd"] - 1), 6)

    if write and (len(dlog) or len(hlog)):
        if len(dlog):
            write_log(dlog, dpath)
        if len(hlog):
            write_log(hlog, hpath)
    for a in added:
        log.info("Signaaliloki: lisätty %s", a)
    return tracking_stats(dlog, hlog, added)


def tracking_stats(dlog: pd.DataFrame, hlog: pd.DataFrame, added: list) -> dict:
    out = dict(enabled=True, n_daily=int(len(dlog)), n_hourly=int(len(hlog)), added=added,
               since=str(dlog["pvm"].iloc[0]) if len(dlog) else None,
               since_hourly=str(hlog["tunti_utc"].iloc[0]) if len(hlog) else None,
               files=[n for n, df in ((LOG_DAILY_FILE, dlog), (LOG_HOURLY_FILE, hlog)) if len(df)],
               horizons=[], recent=[])
    for key, df, col in (("day", hlog, "muutos_4h"), ("swing", dlog, "muutos_5pv"), ("long", dlog, "muutos_40pv")):
        pref = CSV_PREFIX[key]
        item = dict(key=key, name=HZ[key]["name"], ahead=HZ[key]["ahead"], logged=int(len(df)),
                    matured=0, calls=0, hits=0, hit_rate=None, mean_p=None, real_up=None,
                    next=next_result(df, key, col))
        if len(df) and col in df and f"{pref}_nousu" in df:
            sub = df[df[col].notna() & df[f"{pref}_nousu"].notna()]
            item["matured"] = int(len(sub))
            if len(sub):
                net = sub[f"{pref}_nousu"] - sub[f"{pref}_lasku"]
                calls = sub[(net.abs() >= CALL_THRESHOLD) & (sub[col] != 0)]
                cn = net[calls.index]
                item["calls"] = int(len(calls))
                item["hits"] = int(((cn > 0) == (calls[col] > 0)).sum())
                item["hit_rate"] = item["hits"] / item["calls"] if item["calls"] else None
                if f"{pref}_p_nousu" in sub and sub[f"{pref}_p_nousu"].notna().any():
                    item["mean_p"] = float(sub[f"{pref}_p_nousu"].mean())
                item["real_up"] = float((sub[col] > 0).mean())
        out["horizons"].append(item)
    if len(dlog):
        for _, r in dlog.tail(10).iloc[::-1].iterrows():
            def g(c):
                v = r.get(c)
                return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)
            out["recent"].append(dict(pvm=pv(pd.Timestamp(r["pvm"])), price=g("hopea_usd"),
                                      bull=g("lyhyt_nousu"), bear=g("lyhyt_lasku"), p=g("lyhyt_p_nousu"),
                                      chg=g("muutos_5pv")))
    return out


def next_result(df: pd.DataFrame, key: str, col: str) -> str | None:
    """Milloin seuraava kirjattu ennuste ratkeaa (selkokielinen arvio)."""
    if not len(df) or col not in df:
        return None
    pend = df[df[col].isna()]
    if not len(pend):
        return None
    tz = ZoneInfo(TIMEZONE)
    if key == "day":
        t0 = pd.to_datetime(pend["kynttila_utc"], utc=True).min()
        t = (t0 + pd.Timedelta(minutes=HZ["day"]["forward"] * bar_minutes())).tz_convert(tz)
        today = pd.Timestamp.now(tz=tz).normalize()
        when = f"klo {t.hour:02d}.{t.minute:02d}" if t.normalize() == today else pvk(t)
        return f"noin {when}, jos markkina on auki"
    d0 = pd.to_datetime(pend["pvm"]).min()
    d = pd.bdate_range(d0, periods=HZ[key]["forward"] + 1)[-1]
    return f"{d.day}.{d.month}. päätöskurssin jälkeen"


def copy_logs(log_dir: Path, out_dir: Path) -> None:
    import shutil
    if log_dir.resolve() == out_dir.resolve():
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in (LOG_DAILY_FILE, LOG_HOURLY_FILE):
        src = log_dir / name
        if src.exists():
            shutil.copy2(src, out_dir / name)


# ════════════════════════════════════════════════════════════════════════════
#  HTML-SIVUN KIRJOITUS
# ════════════════════════════════════════════════════════════════════════════

def _json_default(o):
    if isinstance(o, (np.integer,)): return int(o)
    if isinstance(o, (np.floating,)): return None if not math.isfinite(o) else float(o)
    if isinstance(o, (np.bool_,)): return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)): return o.isoformat()
    raise TypeError(type(o))


def _clean(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def render(payload: dict, out: Path, template: Path, json_out: Path | None = None) -> None:
    data = json.dumps(_clean(payload), ensure_ascii=False, default=_json_default, separators=(",", ":"))
    html = template.read_text(encoding="utf-8")
    refresh = payload["meta"]["refresh"]
    html = html.replace("__REFRESH_META__",
                        f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else "")
    html = html.replace("__DATA__", data.replace("</", "<\\/"))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(data, encoding="utf-8")


def run_once(args) -> dict:
    md = demo_data() if args.demo else fetch_all()
    an = analyse(md)
    refresh = args.refresh if args.refresh is not None else (args.loop or DEFAULT_PAGE_REFRESH)
    out = Path(args.out)
    log_dir = None if args.no_log else Path(args.log_dir)
    payload = build_payload(md, an, refresh, log_dir=log_dir, write_log=True, out_dir=out.parent)
    render(payload, out, Path(args.template), Path(args.json) if args.json else None)
    p = payload["price"]
    parts = []
    for h in payload["horizons"]:
        if not h.get("available"):
            parts.append(f"{h['name']}: –")
            continue
        side = "▲" if h["bull"] >= h["bear"] else "▼"
        c = h.get("calib")
        prob = f" (P↑ {c['p']*100:.0f} %)" if c else ""
        parts.append(f"{h['name']} {side}{max(h['bull'], h['bear'])*100:.0f} %{prob}{' ◆' if h['signal'] else ''}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Hopea {p['usd']} $ ({p['chg']}) | " + " | ".join(parts), flush=True)
    for a in payload.get("tracking", {}).get("added", []) or []:
        print(f"    + signaaliloki: {a}", flush=True)
    for e in md.errors:
        print(f"    ! {e}", flush=True)
    return payload


def main() -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Hopean markkinaindikaattorit ja signaalisivu.")
    ap.add_argument("--out", default="site/index.html", help="HTML-tiedosto (oletus site/index.html)")
    ap.add_argument("--json", default=None, help="tallenna laskettu data myös JSON-tiedostoon")
    ap.add_argument("--template", default=str(here / "template.html"))
    ap.add_argument("--loop", type=int, default=0, metavar="SEK",
                    help="aja jatkuvasti tällä välillä (esim. 60)")
    ap.add_argument("--refresh", type=int, default=None, metavar="SEK",
                    help="sivun automaattinen uudelleenlataus selaimessa (0 = pois)")
    ap.add_argument("--demo", action="store_true", help="käytä keksittyä dataa (ei verkkoa, ei lokikirjauksia)")
    ap.add_argument("--log-dir", default=LOG_DIR, help=f"signaalilokin kansio (oletus {LOG_DIR})")
    ap.add_argument("--no-log", action="store_true", help="älä lue tai kirjoita signaalilokia")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    if not args.loop:
        try:
            run_once(args)
            print(f"Valmis: {Path(args.out).resolve()}")
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("Ajo epäonnistui: %s", e, exc_info=args.verbose)
            if not args.verbose:
                print("Aja 'python hopea.py -v' nähdäksesi tarkemman virheen.", file=sys.stderr)
            return 1

    print(f"Päivitetään {args.loop} s välein. Avaa {Path(args.out).resolve()} selaimessa. Lopetus: Ctrl+C")
    while True:
        t0 = time.time()
        try:
            run_once(args)
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # noqa: BLE001
            log.error("Ajo epäonnistui: %s", e, exc_info=args.verbose)
        try:
            time.sleep(max(1.0, args.loop - (time.time() - t0)))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
