import json
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

app = FastAPI(title="Döviz Dönüştürücü API", version="1.0.0")


# Sabit (fallback) kur sözlüğü: (from, to) -> rate
# Not: Gerçek zamanlı kurlar dış servisten çekilir; bu sözlük sadece hata durumunda kullanılır.
FALLBACK_RATES: dict[tuple[str, str], float] = {
    ("USD", "TRY"): 32.5,
    ("TRY", "USD"): 1 / 32.5,
    ("EUR", "TRY"): 35.2,
    ("TRY", "EUR"): 1 / 35.2,
    ("EUR", "USD"): 1.09,
    ("USD", "EUR"): 1 / 1.09,
    ("GBP", "TRY"): 55.0,
    ("TRY", "GBP"): 1 / 55.0,
}

# UI'da gösterilecek para birimleri (endpoint daha genişini de kabul edebilir)
GOLD_CODE = "XAU"  # Converter tarafında altın = gram altın (22 ay)
SUPPORTED_CURRENCIES = ["USD", "EUR", "GBP", "TRY", GOLD_CODE]


# Canlı FX ve altın spot fiyatı çekmek için kaynaklar
FX_URL = "https://doviz.dev/v1/try.json"
GOLD_URL = "https://metalmetric.com/api/gpt?action=spot_prices&metal=all"

# Basit in-memory cache (çok sık istek atmayı engeller)
FX_CACHE_TTL_SECONDS = 300
GOLD_CACHE_TTL_SECONDS = 60

_fx_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}
_gold_cache: dict[str, Any] = {"data": None, "fetched_at": 0.0}


async def _fetch_json(url: str) -> Any:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()


async def get_fx_data() -> dict[str, Any]:
    now = time.time()
    if _fx_cache["data"] is not None and (now - _fx_cache["fetched_at"]) < FX_CACHE_TTL_SECONDS:
        return _fx_cache["data"]

    data = await _fetch_json(FX_URL)
    # doviz.dev şu şekildedir: {"USDTRY": 44.4, "TRYUSD": 0.022, ..., "_meta": {...}}
    _fx_cache["data"] = data
    _fx_cache["fetched_at"] = now
    return data


def compute_rate_from_try_pivot(fx_data: dict[str, Any], from_currency: str, to_currency: str) -> float | None:
    """1 from_currency -> to_currency oranını TRY pivotu ile hesaplar."""
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        return 1.0

    if from_currency == "TRY":
        key = f"TRY{to_currency}"
        v = fx_data.get(key)
        return float(v) if v is not None else None

    if to_currency == "TRY":
        key = f"{from_currency}TRY"
        v = fx_data.get(key)
        return float(v) if v is not None else None

    # A -> TRY ve TRY -> B
    key_a_try = f"{from_currency}TRY"
    key_try_b = f"TRY{to_currency}"
    a_try = fx_data.get(key_a_try)
    try_b = fx_data.get(key_try_b)
    if a_try is None or try_b is None:
        return None
    return float(a_try) * float(try_b)


def compute_rate_fallback(from_currency: str, to_currency: str) -> float | None:
    """Canlı çekim başarısızsa sadece demo amaçlı fallback oranlar."""
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        return 1.0

    if from_currency == "TRY":
        return FALLBACK_RATES.get(("TRY", to_currency))

    if to_currency == "TRY":
        return FALLBACK_RATES.get((from_currency, "TRY"))

    a_try = FALLBACK_RATES.get((from_currency, "TRY"))
    try_b = FALLBACK_RATES.get(("TRY", to_currency))
    if a_try is None or try_b is None:
        return None
    return a_try * try_b


async def get_fx_rate(from_currency: str, to_currency: str) -> float | None:
    try:
        fx_data = await get_fx_data()
        rate = compute_rate_from_try_pivot(fx_data, from_currency, to_currency)
        if rate is not None:
            return rate
    except Exception:
        # Canlı kur çekimi patlarsa fallback üzerinden ilerler.
        pass
    return compute_rate_fallback(from_currency, to_currency)


async def get_conversion_rate(from_currency: str, to_currency: str) -> float | None:
    """
    from/to:
      - fiat currency codes: USD/EUR/GBP/TRY
      - GOLD_CODE (XAU): gram altın (22 ay)
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    if from_currency == to_currency:
        return 1.0

    if from_currency == GOLD_CODE or to_currency == GOLD_CODE:
        gold = await get_gold_spot_usd_and_try()
        try_per_gram = gold.get("try_per_gram")
        if try_per_gram is None:
            return None

        # TRY pivotu üzerinden çevir
        if from_currency == GOLD_CODE:
            r_from_try = float(try_per_gram)  # TRY per gram
        else:
            r_from_try = await get_fx_rate(from_currency, "TRY")

        if to_currency == GOLD_CODE:
            r_try_to = 1.0 / float(try_per_gram)  # gram per TRY
        else:
            r_try_to = await get_fx_rate("TRY", to_currency)

        if r_from_try is None or r_try_to is None:
            return None
        return r_from_try * r_try_to

    return await get_fx_rate(from_currency, to_currency)


async def get_gold_spot_usd_and_try() -> dict[str, Any]:
    now = time.time()
    if _gold_cache["data"] is not None and (now - _gold_cache["fetched_at"]) < GOLD_CACHE_TTL_SECONDS:
        return _gold_cache["data"]

    try:
        data = await _fetch_json(GOLD_URL)
    except Exception:
        out = {
            "usd_per_oz": None,
            "try_per_oz": None,
            "unit": "USD/troy oz",
            "source_timestamp": None,
        }
        _gold_cache["data"] = out
        _gold_cache["fetched_at"] = now
        return out

    # MetalMetric: {"prices": {"gold": {"price_per_oz": 4452.37, "unit": "USD/troy oz"}, ...}}
    gold = (data.get("prices") or {}).get("gold") or {}
    usd_per_oz_raw = gold.get("price_per_oz")
    usd_per_oz = float(usd_per_oz_raw) if usd_per_oz_raw is not None else None

    # Altını TRY'ye çevirmek için USD/TRY kurunu aynı FX kaynağından al
    usd_try = await get_fx_rate("USD", "TRY")

    gold_try_per_oz = (
        usd_per_oz * usd_try
        if (usd_per_oz is not None and usd_try is not None)
        else None
    )

    # Troy ounce -> gram dönüşümü
    # Not: MetalMetric "spot" fiyatı ince altın (24 ay) gibi düşünülür.
    # Çeyrek/Tam ve converter tarafında "gram altın" için 22 ay saflığı uygulanır.
    troy_oz_in_grams = 31.1034768
    purity_22k = 22 / 24  # 22 ay = 24 ayın 22/24'ü kadar saf altın

    # Türkiye'de yaygın kullanılan sikke ağırlıkları (standart yaklaşık değerler)
    # - Çeyrek altın: 1.75 g (22 ay)
    # - Tam altın: 7.016 g (22 ay)
    ceyrek_coin_grams = 1.75
    tam_coin_grams = 7.016

    gold_try_per_gram_fine = (
        gold_try_per_oz / troy_oz_in_grams if gold_try_per_oz is not None else None
    )
    gold_try_per_gram = (
        gold_try_per_gram_fine * purity_22k
        if gold_try_per_gram_fine is not None
        else None
    )
    gold_try_ceyrek = gold_try_per_gram * ceyrek_coin_grams if gold_try_per_gram is not None else None
    gold_try_tam = gold_try_per_gram * tam_coin_grams if gold_try_per_gram is not None else None

    out = {
        "usd_per_oz": usd_per_oz,
        "try_per_oz": gold_try_per_oz,
        "try_per_gram": gold_try_per_gram,
        "try_ceyrek": gold_try_ceyrek,
        "try_tam": gold_try_tam,
        "unit": gold.get("unit", "USD/troy oz"),
        "source_timestamp": data.get("timestamp"),
    }

    _gold_cache["data"] = out
    _gold_cache["fetched_at"] = now
    return out


class ConvertQuery(BaseModel):
    amount: float = Field(..., gt=0, description="Dönüştürülecek miktar (pozitif)")
    from_currency: str = Field(..., min_length=3, max_length=3, description="Kaynak para birimi (örn: USD)")
    to_currency: str = Field(..., min_length=3, max_length=3, description="Hedef para birimi (örn: TRY)")

    @field_validator("from_currency", "to_currency")
    @classmethod
    def normalize_currency(cls, v: str) -> str:
        return v.strip().upper()


@app.post("/convert")
async def convert(q: ConvertQuery):
    rate = await get_conversion_rate(q.from_currency, q.to_currency)
    if rate is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Unsupported currency pair",
                "supported_currencies": SUPPORTED_CURRENCIES,
            },
        )

    converted = q.amount * rate
    return {
        "amount": q.amount,
        "from_currency": q.from_currency,
        "to_currency": q.to_currency,
        "rate": rate,
        "converted_amount": round(converted, 6),
    }


@app.get("/prices")
async def prices():
    # FX (GBP/TRY dahil) ve altın fiyatını tek seferde döndür.
    try:
        fx_data = await get_fx_data()
        meta = fx_data.get("_meta") or {}
        fx_updated_at = meta.get("updated_at")
        gbp_try = compute_rate_from_try_pivot(fx_data, "GBP", "TRY") or compute_rate_fallback("GBP", "TRY")
    except Exception:
        fx_updated_at = None
        gbp_try = compute_rate_fallback("GBP", "TRY")

    gold = await get_gold_spot_usd_and_try()
    return {
        "fx_updated_at": fx_updated_at,
        "gbp_try": gbp_try,
        "gold": gold,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    currencies_json = json.dumps(SUPPORTED_CURRENCIES)
    return f"""<!doctype html>
<html lang="tr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Döviz Dönüştürücü</title>
    <style>
      :root {{
        --bg: #0b1220;
        --card: rgba(255, 255, 255, 0.06);
        --card-border: rgba(255, 255, 255, 0.14);
        --text: rgba(255, 255, 255, 0.92);
        --muted: rgba(255, 255, 255, 0.68);
        --primary: #6ee7ff;
        --danger: #fb7185;
        --success: #34d399;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, "Apple Color Emoji",
          "Segoe UI Emoji";
        background: radial-gradient(1000px 600px at 20% 10%, rgba(110, 231, 255, 0.18), transparent),
          radial-gradient(900px 500px at 90% 30%, rgba(52, 211, 153, 0.12), transparent),
          var(--bg);
        color: var(--text);
      }}
      .wrap {{
        width: min(980px, 92vw);
        margin: 0 auto;
        padding: 40px 0;
      }}
      .top {{
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 16px;
        margin-bottom: 18px;
      }}
      .title {{
        font-size: 28px;
        font-weight: 800;
        letter-spacing: -0.02em;
      }}
      .subtitle {{
        margin-top: 6px;
        color: var(--muted);
        line-height: 1.5;
        max-width: 56ch;
      }}
      .card {{
        background: var(--card);
        border: 1px solid var(--card-border);
        border-radius: 18px;
        padding: 18px;
        backdrop-filter: blur(10px);
      }}
      form {{
        display: grid;
        gap: 12px;
      }}
      .row {{
        display: grid;
        grid-template-columns: 1.2fr 1fr 1fr;
        gap: 12px;
      }}
      @media (max-width: 720px) {{
        .row {{ grid-template-columns: 1fr; }}
      }}
      label {{
        display: block;
        font-size: 13px;
        color: var(--muted);
        margin: 0 0 8px 2px;
      }}
      input, select {{
        width: 100%;
        height: 44px;
        border-radius: 12px;
        border: 1px solid rgba(255,255,255,0.18);
        background: rgba(0,0,0,0.20);
        padding: 0 12px;
        color: var(--text);
        outline: none;
      }}
      input:focus, select:focus {{
        border-color: rgba(110, 231, 255, 0.7);
        box-shadow: 0 0 0 3px rgba(110, 231, 255, 0.15);
      }}
      .actions {{
        display: flex;
        gap: 12px;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        margin-top: 6px;
      }}
      .btn {{
        height: 44px;
        padding: 0 16px;
        border-radius: 12px;
        border: 1px solid rgba(110, 231, 255, 0.45);
        background: linear-gradient(180deg, rgba(110, 231, 255, 0.22), rgba(110, 231, 255, 0.10));
        color: var(--text);
        font-weight: 700;
        cursor: pointer;
      }}
      .btn:disabled {{
        opacity: 0.6;
        cursor: not-allowed;
      }}
      .hint {{
        color: var(--muted);
        font-size: 13px;
      }}
      .result {{
        margin-top: 14px;
        padding: 14px;
        border-radius: 14px;
        border: 1px solid rgba(255,255,255,0.14);
        background: rgba(0,0,0,0.18);
        min-height: 76px;
      }}
      .result .k {{
        font-size: 13px;
        color: var(--muted);
      }}
      .result .v {{
        margin-top: 6px;
        font-size: 18px;
        font-weight: 800;
      }}
      .pill {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.14);
        color: var(--muted);
        margin-top: 10px;
        font-size: 13px;
      }}
      .pill strong {{
        color: var(--text);
        font-weight: 800;
      }}
      .marketRow {{
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin-bottom: 14px;
      }}
      .marketRow .pill {{
        margin-top: 0;
      }}
      .lang {{
        display: flex;
        gap: 10px;
        align-items: center;
      }}
      .langBtn {{
        height: 38px;
        width: 52px;
        border-radius: 999px;
        border: 1px solid rgba(255,255,255,0.16);
        background: rgba(0,0,0,0.18);
        color: var(--text);
        cursor: pointer;
        font-size: 18px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }}
      .langBtn.active {{
        border-color: rgba(110, 231, 255, 0.6);
        box-shadow: 0 0 0 3px rgba(110, 231, 255, 0.14);
      }}
      .error {{
        border-color: rgba(251, 113, 133, 0.5);
      }}
      .error .v {{
        color: var(--danger);
      }}
      .footerBrand {{
        position: fixed;
        right: 18px;
        bottom: 18px;
        color: rgba(255,255,255,0.55);
        font-size: 12px;
        background: rgba(0,0,0,0.20);
        border: 1px solid rgba(255,255,255,0.14);
        padding: 10px 12px;
        border-radius: 12px;
        backdrop-filter: blur(10px);
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="top">
        <div>
          <div class="title" id="titleText">Döviz Dönüştürücü</div>
          <div class="subtitle" id="subtitleText">Canlı kurlar ve altın dönüşümü</div>
        </div>

        <div class="lang" aria-label="Dil seçimi">
          <button type="button" class="langBtn" id="langTr" data-lang="tr" aria-label="Türkçe">🇹🇷</button>
          <button type="button" class="langBtn" id="langEn" data-lang="en" aria-label="English">🇬🇧</button>
        </div>
      </div>

      <div class="card">
        <div class="marketRow" aria-label="Güncel piyasa verileri">
          <div class="pill"><span id="gbpTryLabel">GBP/TRY</span>: <strong id="gbpTryText">—</strong></div>
          <div class="pill"><span id="goldGramLabel">Gram altın</span>: <strong id="goldGramTryText">—</strong> <span style="color: var(--muted); font-size: 13px;">TRY</span></div>
          <div class="pill"><span id="goldCeyrekLabel">Çeyrek altın</span>: <strong id="goldCeyrekTryText">—</strong> <span style="color: var(--muted); font-size: 13px;">TRY</span></div>
          <div class="pill"><span id="goldTamLabel">Tam altın</span>: <strong id="goldTamTryText">—</strong> <span style="color: var(--muted); font-size: 13px;">TRY</span></div>
        </div>
        <div class="hint" id="marketHint">Piyasa verileri yükleniyor...</div>
        <form id="convertForm" autocomplete="off">
          <div class="row">
            <div>
              <label for="amount" id="amountLabel">Miktar</label>
              <input id="amount" name="amount" type="number" min="0.000001" step="any" placeholder="Örn: 10" required />
            </div>
            <div>
              <label for="from_currency" id="fromLabel">Kaynak</label>
              <select id="from_currency" name="from_currency" required></select>
            </div>
            <div>
              <label for="to_currency" id="toLabel">Hedef</label>
              <select id="to_currency" name="to_currency" required></select>
            </div>
          </div>

          <div class="actions">
            <button class="btn" id="convertBtn" type="submit">Dönüştür</button>
            <div class="hint" id="exampleHint">Örnek: <span style="color: rgba(255,255,255,0.85); font-weight: 700;">10 USD -> TRY</span></div>
          </div>

          <div class="result" id="resultBox" role="status" aria-live="polite">
            <div class="k" id="resultLabel">Sonuç</div>
            <div class="v" id="resultText">—</div>
            <div class="pill" id="ratePill" style="display:none;">
              <span id="rateLabel">Rate</span>: <strong id="rateText">—</strong>
            </div>
          </div>
        </form>
      </div>
    </div>

    <div class="footerBrand">Developer: <strong>Behram Aras</strong></div>

    <script>
      const currencies = {currencies_json};
      const fromEl = document.getElementById('from_currency');
      const toEl = document.getElementById('to_currency');
      const amountEl = document.getElementById('amount');
      const formEl = document.getElementById('convertForm');
      const btnEl = document.getElementById('convertBtn');
      const resultBox = document.getElementById('resultBox');
      const resultText = document.getElementById('resultText');
      const ratePill = document.getElementById('ratePill');
      const rateText = document.getElementById('rateText');
      const gbpTryText = document.getElementById('gbpTryText');
      const goldGramTryText = document.getElementById('goldGramTryText');
      const goldCeyrekTryText = document.getElementById('goldCeyrekTryText');
      const goldTamTryText = document.getElementById('goldTamTryText');
      const marketHint = document.getElementById('marketHint');

      const titleText = document.getElementById('titleText');
      const subtitleText = document.getElementById('subtitleText');
      const amountLabel = document.getElementById('amountLabel');
      const fromLabel = document.getElementById('fromLabel');
      const toLabel = document.getElementById('toLabel');
      const exampleHint = document.getElementById('exampleHint');
      const resultLabel = document.getElementById('resultLabel');
      const rateLabel = document.getElementById('rateLabel');
      const gbpTryLabel = document.getElementById('gbpTryLabel');
      const goldGramLabel = document.getElementById('goldGramLabel');
      const goldCeyrekLabel = document.getElementById('goldCeyrekLabel');
      const goldTamLabel = document.getElementById('goldTamLabel');
      const langTrBtn = document.getElementById('langTr');
      const langEnBtn = document.getElementById('langEn');

      const i18n = {{
        tr: {{
          title: 'Döviz Dönüştürücü',
          subtitle: 'Canlı kurlar ve altın dönüşümü',
          amount: 'Miktar',
          from: 'Kaynak',
          to: 'Hedef',
          amountPlaceholder: 'Örn: 10',
          convert: 'Dönüştür',
          exampleHtml: 'Örnek: <span style="color: rgba(255,255,255,0.85); font-weight: 700;">10 USD -> TRY</span>',
          result: 'Sonuç',
          rate: 'Rate',
          marketLoading: 'Piyasa verileri yükleniyor...',
          marketUpdated: 'Son güncelleme: ',
          marketUpdatedNo: 'Piyasa verileri güncellendi',
          marketError: 'Piyasa verisi alınamadı',
          errorGeneric: 'Bir hata oluştu.',
          requestFailed: 'İstek başarısız',
          converting: 'Dönüştürülüyor...'
        }},
        en: {{
          title: 'Currency & Gold Converter',
          subtitle: 'Live rates with instant gold conversion',
          amount: 'Amount',
          from: 'From',
          to: 'To',
          amountPlaceholder: 'e.g. 10',
          convert: 'Convert',
          exampleHtml: 'Example: <span style="color: rgba(255,255,255,0.85); font-weight: 700;">10 USD -> TRY</span>',
          result: 'Result',
          rate: 'Rate',
          marketLoading: 'Loading market data...',
          marketUpdated: 'Last updated: ',
          marketUpdatedNo: 'Market data updated',
          marketError: 'Could not fetch market data',
          errorGeneric: 'Something went wrong.',
          requestFailed: 'Request failed',
          converting: 'Converting...'
        }}
      }};

      let lang = localStorage.getItem('lang');
      if (!lang) {{
        lang = ((navigator.language || '').toLowerCase().startsWith('en')) ? 'en' : 'tr';
      }}

      function formatCurrency(code) {{
        if (!code) return code;
        if (code === 'XAU') {{
          return (lang === 'tr') ? 'Altın (gram)' : 'Gold (gram)';
        }}
        return code;
      }}

      function setLang(nextLang) {{
        lang = nextLang;
        localStorage.setItem('lang', lang);

        if (langTrBtn && langEnBtn) {{
          langTrBtn.classList.toggle('active', lang === 'tr');
          langEnBtn.classList.toggle('active', lang === 'en');
        }}

        if (titleText) titleText.textContent = i18n[lang].title;
        if (subtitleText) subtitleText.textContent = i18n[lang].subtitle;
        if (amountLabel) amountLabel.textContent = i18n[lang].amount;
        if (fromLabel) fromLabel.textContent = i18n[lang].from;
        if (toLabel) toLabel.textContent = i18n[lang].to;
        if (exampleHint) exampleHint.innerHTML = i18n[lang].exampleHtml;
        if (resultLabel) resultLabel.textContent = i18n[lang].result;
        if (rateLabel) rateLabel.textContent = i18n[lang].rate;

        if (gbpTryLabel) gbpTryLabel.textContent = i18n[lang].gbpTry || 'GBP/TRY';
        if (goldGramLabel) goldGramLabel.textContent = i18n[lang].goldGram || 'Gram altın';
        if (goldCeyrekLabel) goldCeyrekLabel.textContent = i18n[lang].goldCeyrek || 'Çeyrek altın';
        if (goldTamLabel) goldTamLabel.textContent = i18n[lang].goldTam || 'Tam altın';

        amountEl.placeholder = i18n[lang].amountPlaceholder;
        btnEl.textContent = i18n[lang].convert;

        // Seçim kutularını dil değişimine uygun etiketlerle yeniden doldur.
        const prevFrom = fromEl.value;
        const prevTo = toEl.value;
        while (fromEl.firstChild) fromEl.removeChild(fromEl.firstChild);
        while (toEl.firstChild) toEl.removeChild(toEl.firstChild);
        fillSelect(fromEl);
        fillSelect(toEl);

        // Varsayılanlar
        if (currencies.includes('USD')) fromEl.value = 'USD';
        else if (currencies.length) fromEl.value = currencies[0];

        if (currencies.includes('TRY')) toEl.value = 'TRY';
        else if (currencies.length) toEl.value = currencies[0];

        // Önceki seçim hâlâ geçerliyse koru.
        if (currencies.includes(prevFrom)) fromEl.value = prevFrom;
        if (currencies.includes(prevTo)) toEl.value = prevTo;
      }}

      // Güvenli default çeviriler (pills için)
      i18n.tr.gbpTry = 'GBP/TRY';
      i18n.en.gbpTry = 'GBP/TRY';
      i18n.tr.goldGram = 'Gram altın';
      i18n.en.goldGram = 'Gold (gram)';
      i18n.tr.goldCeyrek = 'Çeyrek altın';
      i18n.en.goldCeyrek = 'Quarter gold';
      i18n.tr.goldTam = 'Tam altın';
      i18n.en.goldTam = 'Full gold';

      if (langTrBtn) langTrBtn.addEventListener('click', () => setLang('tr'));
      if (langEnBtn) langEnBtn.addEventListener('click', () => setLang('en'));

      setLang(lang);

      async function loadPrices() {{
        if (!marketHint || !gbpTryText || !goldGramTryText || !goldCeyrekTryText || !goldTamTryText) return;
        marketHint.textContent = i18n[lang].marketLoading;
        try {{
          const resp = await fetch('/prices');
          const data = await resp.json().catch(() => ({{}}));

          if (!resp.ok) {{
            throw new Error(data?.error || i18n[lang].marketError);
          }}

          if (data?.gbp_try !== undefined && data?.gbp_try !== null) {{
            gbpTryText.textContent = Number(data.gbp_try).toFixed(4);
          }} else {{
            gbpTryText.textContent = '—';
          }}

          const goldGramTry = data?.gold?.try_per_gram;
          if (goldGramTry !== undefined && goldGramTry !== null) {{
            goldGramTryText.textContent = Number(goldGramTry).toFixed(2);
          }} else {{
            goldGramTryText.textContent = '—';
          }}

          const goldCeyrekTry = data?.gold?.try_ceyrek;
          if (goldCeyrekTry !== undefined && goldCeyrekTry !== null) {{
            goldCeyrekTryText.textContent = Number(goldCeyrekTry).toFixed(2);
          }} else {{
            goldCeyrekTryText.textContent = '—';
          }}

          const goldTamTry = data?.gold?.try_tam;
          if (goldTamTry !== undefined && goldTamTry !== null) {{
            goldTamTryText.textContent = Number(goldTamTry).toFixed(2);
          }} else {{
            goldTamTryText.textContent = '—';
          }}

          if (data?.fx_updated_at) {{
            marketHint.textContent = i18n[lang].marketUpdated + data.fx_updated_at;
          }} else {{
            marketHint.textContent = i18n[lang].marketUpdatedNo;
          }}
        }} catch (err) {{
          marketHint.textContent = err?.message || i18n[lang].marketError;
        }}
      }}

      loadPrices();
      setInterval(loadPrices, 60000);

      function fillSelect(selectEl) {{
        for (const c of currencies) {{
          const opt = document.createElement('option');
          opt.value = c;
          opt.textContent = formatCurrency(c);
          selectEl.appendChild(opt);
        }}
      }}

      // Dil değişimi ve başlangıç seçimleri `setLang()` içinde yapılıyor.

      formEl.addEventListener('submit', async (e) => {{
        e.preventDefault();
        btnEl.disabled = true;
        resultBox.classList.remove('error');
        ratePill.style.display = 'none';
        resultText.textContent = i18n[lang].converting;

        const payload = {{
          amount: Number(amountEl.value),
          from_currency: fromEl.value,
          to_currency: toEl.value
        }};

        try {{
          const resp = await fetch('/convert', {{
            method: 'POST',
            headers: {{
              'Content-Type': 'application/json'
            }},
            body: JSON.stringify(payload)
          }});

          const data = await resp.json().catch(() => ({{}}));

          if (!resp.ok) {{
            const msg =
              data?.detail?.error ||
              data?.detail ||
              i18n[lang].errorGeneric;
            throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
          }}

          const converted = data.converted_amount;
          const rate = data.rate;

          resultText.textContent = `${{payload.amount}} ${{formatCurrency(data.from_currency)}} = ${{converted}} ${{formatCurrency(data.to_currency)}}`;
          rateText.textContent = String(rate);
          ratePill.style.display = 'inline-flex';
        }} catch (err) {{
          resultBox.classList.add('error');
          resultText.textContent = err?.message || i18n[lang].requestFailed;
        }} finally {{
          btnEl.disabled = false;
        }}
      }});
    </script>
  </body>
</html>
"""
