🌐 **Live Demo:** [currency-converter-api-csh3.onrender.com](https://currency-converter-api-csh3.onrender.com)

# 💱 Currency & Gold Converter

A full-stack **FastAPI** application that provides real-time currency conversion between fiat currencies and gold (XAU), served with a built-in dark-themed web UI — no frontend framework required.

Built with [Cursor](https://cursor.sh/).

---

## ✨ Features

- **Real-time exchange rates** — fetched from [doviz.dev](https://doviz.dev) using TRY as the pivot currency
- **Gold price support** — spot gold prices from [MetalMetric](https://metalmetric.com), converted to:
  - Price per gram (22-karat)
  - Çeyrek altın (quarter gold coin, ~1.75 g)
  - Tam altın (full gold coin, ~7.016 g)
- **Supported currencies:** USD, EUR, GBP, TRY, XAU (gold)
- **Fallback rates** — static rates kick in automatically if the live data source is unavailable
- **In-memory caching** — FX rates cached for 5 minutes, gold prices for 1 minute
- **Bilingual UI** — interface language switches between Turkish 🇹🇷 and English 🇬🇧, auto-detected from the browser
- **Responsive design** — works on desktop and mobile
- **Live market ticker** — GBP/TRY rate and gold prices refresh every 60 seconds on the page

---

## 🖥️ UI Overview

The single-page web app (`GET /`) renders directly from Python as an HTML response. No separate frontend build step is needed.

- Dark glassmorphism card design
- Amount + source/target currency dropdowns
- Instant conversion result with exchange rate pill
- Market data bar showing GBP/TRY and gold prices (gram, çeyrek, tam)
- TR / EN language toggle (persisted in `localStorage`)

---

## 🔌 API Endpoints

### `POST /convert`

Convert an amount from one currency to another.

**Request body:**
```json
{
  "amount": 100,
  "from_currency": "USD",
  "to_currency": "TRY"
}
```

**Response:**
```json
{
  "amount": 100,
  "from_currency": "USD",
  "to_currency": "TRY",
  "rate": 44.4,
  "converted_amount": 4440.0
}
```

Supports all fiat pairs (USD, EUR, GBP, TRY) and gold (`XAU`). Returns `400` if the pair is unsupported.

---

### `GET /prices`

Returns current market snapshot: GBP/TRY rate, gold prices (per oz, per gram, çeyrek, tam), and last FX update timestamp.

**Response:**
```json
{
  "fx_updated_at": "2025-01-01T12:00:00Z",
  "gbp_try": 44.12,
  "gold": {
    "usd_per_oz": 2350.0,
    "try_per_oz": 104340.0,
    "try_per_gram": 2918.5,
    "try_ceyrek": 5107.4,
    "try_tam": 20473.5,
    "unit": "USD/troy oz",
    "source_timestamp": "..."
  }
}
```

---

### `GET /`

Serves the full web UI as an HTML page.

---

## 🏗️ Project Structure

```
app.py          # Everything: FastAPI app, business logic, and HTML UI
```

This is intentionally a single-file project for simplicity and easy deployment.

---

## ⚙️ How Exchange Rates Work

All conversions use **TRY as the pivot currency**:

```
USD → EUR  =  (USD → TRY) × (TRY → EUR)
```

This means only one API source is needed (`USDTRY`, `TRYGBP`, etc.) and any pair can be computed cross-rate.

For gold (`XAU`), the spot price in USD/troy oz is fetched from MetalMetric, then:
1. Converted to TRY using the live USD/TRY rate
2. Converted from troy oz to grams (÷ 31.1034768)
3. Adjusted for 22-karat purity (× 22/24)

---

## 🚀 Getting Started

### Prerequisites

- Python 3.11+
- pip

### Installation

```bash
pip install fastapi uvicorn httpx pydantic
```

### Run

```bash
uvicorn app:app --reload
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

### Run in production

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## 🌐 External Data Sources

| Source | Data | Cache TTL |
|--------|------|-----------|
| [doviz.dev/v1/try.json](https://doviz.dev/v1/try.json) | FX rates (TRY-based) | 5 minutes |
| [metalmetric.com](https://metalmetric.com/api/gpt?action=spot_prices&metal=all) | Gold spot price (USD/oz) | 1 minute |

If either source is unreachable, the app falls back to hardcoded approximate rates and logs no crash.

---

## 🛡️ Input Validation

Currency codes are validated by Pydantic:
- Must be exactly 3 characters
- Automatically uppercased and stripped
- Amount must be a positive number

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` | Web framework & API |
| `uvicorn` | ASGI server |
| `httpx` | Async HTTP client for external APIs |
| `pydantic` | Request validation & data models |
