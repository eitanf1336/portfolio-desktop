# Portfolio Desktop Widget

A transparent, frameless glass widget that floats on the GNOME desktop showing a
**live stock portfolio in shekels (₪)**. Compact by default; click a holding (or
the total) to expand into a graph-rich detail view. Money amounts are **blurred
for privacy** unless you reveal them.

## Controls
- **Ctrl+Shift+P** — show / hide the widget
- **🙈 / 👁 eye button** — reveal / hide all amounts (starts hidden every launch)
- **Click a blurred amount** — peek that one value; click again to re-blur
- **Click a holding** — expand to detail (live chart, 1D/1W/1M/1Y/MAX, position P&L, market stats)
- **Click the total** — portfolio overview (allocation, top movers, income, value chart)
- **✎** — open `~/.config/portfolio/holdings.json` to edit holdings
- **Esc** — collapse a detail view / hide the widget

## Files
- `portfolio.py` — GTK3 + WebKit2 transparent window, positioning, live refresh, JS bridge
- `datafeed.py` — Yahoo Finance data + portfolio math (run `python3 datafeed.py` to test)
- `ui.html` — the UI (HTML/CSS/JS, Chart.js in `vendor/`)
- `~/.config/portfolio/holdings.json` — **your holdings** (edit freely)
- Launcher: `~/.local/bin/portfolio`  ·  Autostart: `~/.config/autostart/portfolio-widget.desktop`

## Holdings model
Two kinds in `holdings.json`:
- **equity** — a real Yahoo ticker (e.g. `GOOG`, `NASA`) in USD; ₪ value = shares × price × live USD/ILS.
- **fund** — an Israeli Nasdaq-100 tracker not on Yahoo (e.g. `5131644`, `5127766`).
  It rides the live **^NDX** index: `hedged:true` follows NDX directly, `hedged:false`
  follows NDX × USD/ILS. `value_ils` is anchored to `ref_proxy`/`ref_fx` captured when recorded.

To re-anchor a fund after a new statement, update its `value_ils`, `cost_ils`,
`ref_proxy` (current ^NDX) and `ref_fx` (current USD/ILS) — or just ask Claude.

## Data source
Yahoo Finance public `v8/finance/chart` endpoint — no API key, refreshed every 60s.
