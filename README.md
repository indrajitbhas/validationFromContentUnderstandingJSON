# PDF Review Tool (Azure Content Understanding)

Local 3-pane web app for reviewing and correcting OCR/extraction output from **Azure Content Understanding**.

> This UI expects **Azure Content Understanding document output only** (top-level `pages[]` with `words[]`).

## What it does

- **Pane 1 (left):** raw analysis JSON (read-only, wrapped text, independent scrolling), hidden by default and shown with the **View analysis** button. Inside that pane, the raw JSON box itself is also collapsible.
- **Pane 2 (middle):** PDF viewer using **vendored PDF.js** (no CDN), with:
  - prev/next page
  - page number input
  - zoom in/out
  - fit width
  - single-page rendering (only current visible page is rendered)
- **Pane 3 (right):** editable curated JSON + page-level editor controls:
  - page `Doc. Type` dropdown (`blank | letter | memorandum | telegram | report | interview | envelope | folder_cover | book_cover | newspaper_clipping | form | FOIA_release_page | routing_slip | index_or_list | drawing_or_sketch | photograph | map | handwritten_note | other`)
  - page `status` (`pending | approved`)
  - hidden page-level corrected text field (still maintained and saved into curated JSON)
  - JSON validation on every edit (line/column parse errors shown)
  - Save disabled while JSON invalid
  - curated JSON box is collapsible

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

## Correcting a word

In the right pane, open **Words on this page**. Each word is an editable box showing
its confidence; low-confidence words are highlighted.

- Typing in a word box updates `words[].text` **and** rewrites that word in
  `page.text` in place, so the searchable page text always matches your correction.
- The `page.text` value is now hidden from the UI, but still synchronized and saved.
- `words[].original` keeps the raw Azure value, so the audit trail is preserved.
- If a word can no longer be located in `page.text`, the word is still saved and a
  warning indicates that manual JSON touch-up may be needed.

## Input format

The tool expects **Azure Content Understanding** document-analysis output. Three shapes are accepted:

- bare form: top-level `"pages": [...]`
- envelope: `{ "kind": "document", "document": { "pages": [...] } }`
- **SDK as_dict() output** (what this project's CLI saves, api 2025-11-01):
  `{ "analyzerId": ..., "apiVersion": ..., "contents": [ { "kind": "document", "pages": [...] } ], ... }`
  — pages are merged across all `contents` entries and sorted by page number.

If loading fails, the error message lists the top-level keys found in your file, which tells you which shape it actually is.
