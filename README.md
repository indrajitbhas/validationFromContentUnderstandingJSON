# PDF Review Tool (Azure Content Understanding)

Local 3-pane web app for reviewing and correcting OCR/extraction output from **Azure Content Understanding**.

> This UI expects **Azure Content Understanding document output only** (top-level `pages[]` with `words[]`).

## What it does

- **Pane 1 (left):** raw analysis JSON (read-only, wrapped text, independent scrolling).
- **Pane 2 (middle):** PDF viewer using **vendored PDF.js** (no CDN), with:
  - prev/next page
  - page number input
  - zoom in/out
  - fit width
  - single-page rendering (only current visible page is rendered)
- **Pane 3 (right):** editable curated JSON + page-level editor controls:
  - page `kind` dropdown (`text | blank | handwritten | seal | screenshot | unknown`)
  - page `status` (`pending | approved`)
  - page-level corrected full text
  - JSON validation on every edit (line/column parse errors shown)
  - Save disabled while JSON invalid

### Issues/filters panel

- Word confidence threshold dropdown (`0.50` to `0.95` in `0.05` steps, default `0.80`)
- Flag empty pages checkbox
- Page-average confidence threshold filter
- Optional issue text search
- Issues list columns: page, snippet, confidence, reason
- Click issue row => jumps PDF viewer to the page

## Save behavior

The app supports two load modes:

1. **Server-folder mode (recommended):**
   - Python server exposes a folder (default: startup folder).
   - UI lists PDF + sibling JSON pairs.
   - Save writes: `<pdfname>.curated.json` in the **same folder as the PDF**.
   - If file exists, a timestamped backup is written first:
     - `<pdfname>.curated.json.YYYYMMDD-HHMMSS.bak`

2. **Open files fallback (browser file picker):**
   - User picks PDF + JSON via browser.
   - Save posts to backend with output folder/file controls.
   - If output folder omitted, server base folder is used.
   - UI message reports exact saved path.

## Project layout

```text
pdf_review_tool/
  app.py
  requirements.txt
  README.md
  fixtures/
    sample_review.pdf
    sample_review.pdf.json
  templates/
    index.html
  static/
    css/styles.css
    js/main.js
    vendor/pdfjs/
      pdf.mjs
      pdf.worker.mjs
```

## Windows (primary) — run locally

Requirements:
- Python 3.10+
- Chrome or Edge

In PowerShell (from project folder):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py --folder . --host 127.0.0.1 --port 8000
```

Open in browser:

- `http://localhost:8000`

Then load the fixture via server mode:
- `fixtures/sample_review.pdf`
- `fixtures/sample_review.pdf.json`

## Ubuntu / dev VM

```bash
cd /home/ubuntu/pdf_review_tool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py --folder . --host 127.0.0.1 --port 8000
```

Open:
- `http://localhost:8000`

## Notes

- This tool intentionally skips bounding box overlays.
- JSON structure checks are strict enough to reject non-Content-Understanding JSON with clear errors.
- Runtime has no CDN dependency; PDF.js assets are vendored in `static/vendor/pdfjs/`.
