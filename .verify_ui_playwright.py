import json
from pathlib import Path

from playwright.sync_api import sync_playwright

EXPECTED_DOC_TYPES = [
  "blank","letter","memorandum","telegram","report","interview","envelope","folder_cover","book_cover","newspaper_clipping","form","FOIA_release_page","routing_slip","index_or_list","drawing_or_sketch","photograph","map","handwritten_note","other"
]

out={"checks":{},"failures":[]}


def check(cond,msg):
    if not cond:
        out['failures'].append(msg)


fixture_path = Path('/home/ubuntu/pdf_review_tool/fixtures/sample_review.curated.json')
fixture_snapshot = fixture_path.read_bytes() if fixture_path.exists() else None

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto('http://127.0.0.1:8011', wait_until='networkidle')

        # Defensive cleanup to avoid any stale persisted state in a reused browser profile.
        page.evaluate('''() => { try { localStorage.clear(); } catch {} }''')
        page.reload(wait_until='networkidle')

        out['checks']['pageTextHiddenInitial'] = page.evaluate('''() => {
          const el = document.getElementById('cur-page-text');
          if (!el) return {exists:false,visible:false};
          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          const visible = !(cs.display === 'none' || cs.visibility === 'hidden' || r.width === 0 || r.height === 0);
          return {exists:true,visible};
        }''')
        check(out['checks']['pageTextHiddenInitial']['exists'], 'Hidden page text textarea element missing.')
        check(not out['checks']['pageTextHiddenInitial']['visible'], 'Page corrected text textarea is visible on initial load.')

        page.click('#load-server-doc')
        page.wait_for_timeout(1400)

        out['checks']['pageTextHiddenAfterLoad'] = page.evaluate('''() => {
          const el = document.getElementById('cur-page-text');
          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          const visible = !(cs.display === 'none' || cs.visibility === 'hidden' || r.width === 0 || r.height === 0);
          return {visible};
        }''')
        check(not out['checks']['pageTextHiddenAfterLoad']['visible'], 'Page corrected text textarea is visible after loading sample file.')

        out['checks']['wordEditSync'] = page.evaluate('''() => {
          const textarea = document.getElementById('curated-editor');
          const wordInput = document.querySelector('#word-list input[data-word-index="0"]');
          if (!wordInput || !textarea) return {ok:false,reason:'word input or curated editor missing'};
          const beforeObj = JSON.parse(textarea.value);
          const selectedPage = Number(document.getElementById('cur-page-select').value || 1);
          const beforePage = (beforeObj.pages||[]).find(p=>Number(p.pageNumber)===selectedPage) || (beforeObj.pages||[])[0];
          if (!beforePage || !Array.isArray(beforePage.words) || beforePage.words.length===0) return {ok:false,reason:'no words'};
          const oldWord = String(beforePage.words[0].text || '');
          const newWord = oldWord + '_X';
          wordInput.value = newWord;
          wordInput.dispatchEvent(new Event('input', {bubbles:true}));
          const afterObj = JSON.parse(textarea.value);
          const afterPage = (afterObj.pages||[]).find(p=>Number(p.pageNumber)===selectedPage) || (afterObj.pages||[])[0];
          const afterWord = String(afterPage.words?.[0]?.text || '');
          const beforeText = String(beforePage.text || '');
          const afterText = String(afterPage.text || '');
          return {ok: afterWord===newWord && afterText!==beforeText && afterText.includes(newWord), oldWord,newWord,afterWord,beforeText,afterText};
        }''')
        check(out['checks']['wordEditSync']['ok'], f"Word edit did not sync into page.text/curated JSON ({out['checks']['wordEditSync'].get('reason','mismatch')}).")

        out['checks']['docTypeLabel'] = (page.text_content('label[for="cur-page-kind"]') or '').strip()
        check(out['checks']['docTypeLabel'] == 'Doc. Type:', 'Doc. Type label is not updated.')

        out['checks']['docTypeListExact'] = page.evaluate("""() => Array.from(document.querySelectorAll('#cur-page-kind option')).map(o=>o.value)""")
        check(out['checks']['docTypeListExact'] == EXPECTED_DOC_TYPES, 'Doc. Type options do not match exact required values.')

        page.click('#toggle-analysis')
        page.wait_for_timeout(200)

        out['checks']['rawJsonCollapseCycle'] = page.evaluate('''() => {
          const panel = document.getElementById('raw-json-panel');
          const toggle = document.getElementById('raw-json-toggle');
          const before = panel.classList.contains('collapsed');
          toggle.click();
          const afterCollapse = panel.classList.contains('collapsed');
          const storage = localStorage.getItem('pdf_review_tool:raw_json_collapsed');
          toggle.click();
          const afterExpand = panel.classList.contains('collapsed');
          return {before,afterCollapse,afterExpand,storageAfterCollapse:storage};
        }''')
        c = out['checks']['rawJsonCollapseCycle']
        check(c['afterCollapse'] is True, 'Raw JSON box did not collapse.')
        check(c['afterExpand'] is False, 'Raw JSON box did not re-expand.')
        check(c['storageAfterCollapse'] == 'true', 'Raw JSON collapsed state not stored in localStorage.')

        page.evaluate('''() => {
          const panel = document.getElementById('raw-json-panel');
          const toggle = document.getElementById('raw-json-toggle');
          if (!panel.classList.contains('collapsed')) toggle.click();
        }''')
        page.reload(wait_until='networkidle')
        page.click('#toggle-analysis')
        page.wait_for_timeout(250)
        out['checks']['rawJsonPersistAfterReload'] = page.evaluate('''() => {
          const panel = document.getElementById('raw-json-panel');
          const toggle = document.getElementById('raw-json-toggle');
          const collapsed = panel.classList.contains('collapsed');
          if (collapsed) toggle.click();
          return {collapsed, stored: localStorage.getItem('pdf_review_tool:raw_json_collapsed')};
        }''')
        check(out['checks']['rawJsonPersistAfterReload']['collapsed'] is True, 'Raw JSON collapsed state did not persist after reload.')

        out['checks']['heightGrowth'] = page.evaluate('''() => {
          const panes = document.getElementById('panes');
          const before = panes.getBoundingClientRect();
          const viewport = window.innerHeight;

          const loadToggle = document.querySelector('.collapsible-panel[data-panel-key="load-panel"] .panel-toggle');
          const issuesToggle = document.querySelector('.collapsible-panel[data-panel-key="issues-panel"] .panel-toggle');
          const loadPanel = document.querySelector('.collapsible-panel[data-panel-key="load-panel"]');
          const issuesPanel = document.querySelector('.collapsible-panel[data-panel-key="issues-panel"]');

          if (!loadPanel.classList.contains('collapsed')) loadToggle.click();
          if (!issuesPanel.classList.contains('collapsed')) issuesToggle.click();
          const after = panes.getBoundingClientRect();
          if (loadPanel.classList.contains('collapsed')) loadToggle.click();
          if (issuesPanel.classList.contains('collapsed')) issuesToggle.click();

          return {beforeHeight: before.height, afterHeight: after.height, grew: after.height > before.height, fillsBottomGap: Math.abs(viewport - after.bottom) <= 2};
        }''')
        check(out['checks']['heightGrowth']['grew'], 'Main panes did not grow taller after collapsing top sections.')
        check(out['checks']['heightGrowth']['fillsBottomGap'], 'Main panes do not fill remaining vertical space after collapse.')

        out['checks']['preserveUnknownKind'] = page.evaluate('''async () => {
          document.getElementById('load-server-doc').click();
          await new Promise(r=>setTimeout(r,1200));

          const editor = document.getElementById('curated-editor');
          const curated = JSON.parse(editor.value);
          if (!curated.pages || !curated.pages.length) return {ok:false,reason:'no pages'};

          const pageNum = Number(curated.pages[0].pageNumber || 1);
          curated.pages[0].kind = 'legacy_custom_kind';
          editor.value = JSON.stringify(curated, null, 2);
          editor.dispatchEvent(new Event('input', {bubbles:true}));

          document.getElementById('save-curated').click();
          await new Promise(r=>setTimeout(r,900));

          document.getElementById('load-server-doc').click();
          await new Promise(r=>setTimeout(r,1200));

          document.getElementById('cur-page-select').value = String(pageNum);
          document.getElementById('cur-page-select').dispatchEvent(new Event('change', {bubbles:true}));

          const values = Array.from(document.querySelectorAll('#cur-page-kind option')).map(o=>o.value);
          const selected = document.getElementById('cur-page-kind').value;
          return {ok: values.includes('legacy_custom_kind') && selected === 'legacy_custom_kind', values, selected};
        }''')
        check(out['checks']['preserveUnknownKind']['ok'], 'Unknown existing Doc. Type value was not preserved/selected.')

        browser.close()
finally:
    if fixture_snapshot is not None:
        fixture_path.write_bytes(fixture_snapshot)

Path('/home/ubuntu/pdf_review_tool/verification_ui.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
print('failures', len(out['failures']))
if out['failures']:
    print('\n'.join(out['failures']))
    raise SystemExit(2)
