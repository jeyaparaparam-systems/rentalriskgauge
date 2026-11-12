# Rental Risk Gauge – SF Bay Area

**www.rentalriskgauge.com** · An independently developed market-risk indicator for San Francisco Bay Area rental property owners. It measures current conditions; it is not a forecast of future prices or rents.

Two scores from 0 (low risk) to 100 (high risk). Levels: Low under 35, Moderate 35–45, Elevated 45–65, High 65 and above.

Each score has six **parts** (each 0–100). Final score = 75% × weighted average of the parts + 25% × average of the two highest parts (scoring model v5.3), so one or two severe problems are not hidden by calm readings elsewhere. **Licensed sources are the main data where available** (S&P Cotality Case-Shiller, Moody's Baa, Nasdaq-100, CoStar cap rate), used for non-commercial purposes while permission requests are pending. Every licensed measure has a free public fallback that is always kept current, and one setting switches the whole site to free data.

- **Single-family homes and condos**: mortgage financing (rate and 1-year change) 20%, market & credit stress (VIX 20-day average 50%, Baa spread 25% [fallback: high-grade spread], Nasdaq-100 drawdown 25%) 15%, San Francisco Bay Area jobs (unemployment, tech jobs, all jobs) 25%, homes for sale (months of supply [fallback: listings per pending sale], listings vs a year ago, price cuts) 15%, home prices (50% Case-Shiller San Francisco [fallback: FHFA], 50% FHFA San Jose) 10%, valuation (price-to-rent, same 50/50 split) 15%.
- **Multifamily apartments (5+ units)**: apartment financing (10-year Treasury yield, bank lending standards, Baa spread) 25%, value cushion (CoStar cap rate minus the 10-year yield, 0 risk at a 3-point gap and 100 at 0.5 points [fallback: price-to-rent]) 15%, jobs 20%, vacancy (Apartment List vacancy index, San Francisco + San Jose metros [fallback: California rental vacancy vs its average]) 20%, rent growth (50% Apartment List rent on new leases + 50% BLS rent paid) 10%, new construction (24-month building permits vs their 2005–2025 average pace) 10%.
- **Context** (shown, not scored): California WARN layoff notices for the five core counties (last 30 days); insurance price trends; "Today in context" (today vs history since 2005 and distance to the next risk level).

The full methodology is on the site's Methodology page.

## How the site works

| Path | What it is |
|---|---|
| `index.html` | The whole website (one file). It reads the two data files below. |
| `data/latest.json` | Today's numbers, details and the short "Today's read" note. |
| `data/history.json` | One row per daily update (feeds the chart after November 5, 2026). |
| `data/manual.json` | The one hand-entered figure: the CoStar apartment cap rate (quarterly). The workflow opens a reminder issue when it is more than about four months old. |
| `scripts/update.py` | Downloads the data, recalculates the scores, writes the two data files. |
| `.github/workflows/daily-update.yml` | Runs the script every market day after the close and saves the result. |
| `og-image.png` | Picture shown when the site is shared on social media or in messages. |
| `robots.txt`, `sitemap.xml` | Help search engines find and index the site. |
| `404.html` | Friendly "page not found" page. |
| `_headers` | Security and caching headers (used by Cloudflare Pages). |

History before November 5, 2026 (monthly, since 2000) is built into `index.html`.

## Daily updates

**Fail-safe setup.** The workflow runs at about 4:45 PM PST and again at about 6:20 PM and 8:40 PM PST as backups (a backup does nothing if today's data is already saved). Each run retries every download up to 3 times and retries the whole update once after 3 minutes. The last evening run and a 7:10 AM PST morning check confirm the saved data is current. If anything fails, the workflow opens a GitHub issue titled "Daily data update failed" (GitHub emails you about new issues), and closes it automatically after the next good run. The website itself shows "Latest available data: [date]. Some market indicators may be delayed." whenever an update is missing, and never shows a next-update date in that case.


GitHub Actions runs `scripts/update.py` Monday–Friday evening (4:45 PM PST / 5:45 PM PDT), skips U.S. stock market holidays, and commits the new data. The hosting service (Cloudflare Pages or GitHub Pages) republishes the site automatically.

Licensed sources (non-commercial use, permission pending): Case-Shiller `SFXRSA`, `CSUSHPINSA`; Moody's `BAA10Y`; Nasdaq-100 `NASDAQ100` (all via FRED); CoStar cap rate from `data/manual.json`.

Free monthly market data, downloaded automatically: Apartment List Vacancy Index and Rent Estimates (apartmentlist.com/research/category/data-rent-estimates; download links read from that page each run), and Redfin Data Center county market tracker (months of supply, Santa Clara County single-family homes; read on Tuesdays because the file is large).

Free public sources downloaded automatically (credit each source as on the site's Sources page):

- VIX daily close – Cboe (fallback: FRED `VIXCLS`)
- 10-year Treasury yield – U.S. Treasury daily yield curve (fallback: FRED `DGS10`); monthly `GS10`
- Corporate bond spread – U.S. Treasury HQM corporate yield `HQMCB10YR` minus `GS10` (monthly)
- 30-year mortgage rate – Freddie Mac via FRED `MORTGAGE30US` (weekly, Thursdays)
- Unemployment – BLS seasonally adjusted via FRED `SANF806UR`, `SANJ906UR`, weighted by labor force `SANF806LF`, `SANJ906LF`; U.S. rate `UNRATE` (monthly)
- Tech jobs and all jobs – BLS via FRED `SANF806INFON`, `SANJ906INFON`, `SANF806NAN`, `SANJ906NAN` (monthly)
- Home prices – FHFA all-transactions HPI via FRED `ATNHPIUS41884Q`, `ATNHPIUS36084Q`, `ATNHPIUS41940Q`, averaged (quarterly)
- Rents paid – BLS CPI rent, San Francisco area, FRED `CUURA422SEHA` (monthly)
- Price-to-rent – FHFA average ÷ `CUURA422SEHA`, divided by its 2005–2025 average (0.92152)
- Homes for sale – Realtor.com via FRED `ACTLISCOU6085`, `PENLISCOU6085`, `PRIREDCOU6085` (monthly)
- Rental vacancy – Census via FRED `CARVAC` (yearly, California)
- Building permits – Census via FRED `SANF806BPPRIV`, `SANJ906BPPRIV` (monthly; 24-month total ÷ 33,101 × 100)
- Bank lending standards for apartment loans – Federal Reserve survey via FRED `SUBLPDRCSMLGNQ` (quarterly)
- Insurance prices (context) – FRED `CUUR0000SEHD` (monthly)
- Layoff notices – California EDD WARN report (`warn_report1.xlsx`, read with openpyxl)

If a source is down, the previous value is kept (missing measures are re-weighted within their part) and the site shows its "as of" date. Obviously broken numbers are rejected.

**Licensed data switch.** If a data provider declines permission, either delete that provider's values from `data/manual.json` (CoStar) or set the repository variable / workflow env `USE_LICENSED_DATA=0`, and in `index.html` change `let PREMIUM=true;` to `let PREMIUM=false;`. The page and the scores then use the free public measures only (adding `?data=public` to the address previews this). Also remove that provider's credit line from the Sources page.

## One-time setup

1. **FRED API key** – free at https://fred.stlouisfed.org/docs/api/api_key.html. In this repository: *Settings → Secrets and variables → Actions → New repository secret*, name `FRED_API_KEY`.
2. **Allow the workflow to save data** – *Settings → Actions → General → Workflow permissions → Read and write permissions*.
3. **Test it** – *Actions → Daily data update → Run workflow*. A new commit "Daily data update …" should appear.
4. **Hosting** (pick one):
   - **Cloudflare Pages** (works with a private repo): Create project → Connect to Git → this repo → Framework preset *None*, build command empty, output directory `/`. Then *Custom domains* → `www.rentalriskgauge.com`.
   - **GitHub Pages** (repo must be public on a free plan): *Settings → Pages → Deploy from a branch → main / root*. The `CNAME` file already sets `www.rentalriskgauge.com`.
5. **DNS at your domain registrar** – point `www` to your host (Cloudflare shows the exact record; for GitHub Pages it is a CNAME to `<owner>.github.io`), and forward the bare domain `rentalriskgauge.com` to `www`.

## Checking the scores locally

```
python scripts/update.py --check     # recompute today's scores from the saved data, no downloads
```

---

© 2026 Raj Rajagopalan. All rights reserved. For informational purposes only; not financial, investment or legal advice. Data belongs to its respective providers.

## Before launch checklist

1. Add the `FRED_API_KEY` secret and allow workflow write access (see One-time setup).
2. Run *Actions → Daily data update → Run workflow* once and confirm a "Daily data update" commit appears and the site shows today's date.
3. Connect hosting and the `www.rentalriskgauge.com` domain; check that `https://www.rentalriskgauge.com/data/latest.json` loads.
4. Share the link in a message to yourself to check the preview picture.
5. Have a lawyer review the disclaimer and data credits, especially if you add ads or paid features.

## Privacy and fonts

The site sets no cookies and uses no analytics or tracking. It saves only display choices (last tab, selected indicator, theme, stress-test weights) in the visitor's browser `localStorage`. Fonts (IBM Plex Sans, IBM Plex Mono, Source Serif 4; SIL Open Font License, see `fonts/`) are served from this site, so no visitor data goes to a font service.
