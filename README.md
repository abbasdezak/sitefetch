# sitefetch

Best-effort website fetcher + analyzer built for rating contractor/SME lead
sites (UI + UX quality) without a browser. It fetches the way review rules
expect (protocol fallbacks, bot-UA retry on blocks, redirect-hijack
detection), then extracts everything an LLM needs to judge a site and writes
an LLM-friendly digest.

Built for the lead-sheet cold-email pipeline (see `/root/web2/glm/REVIEW-PLAYBOOK.md`)
but usable standalone on any site.

---

## 1. Quick start

```bash
# from /root/web2/glm
./fetchsite example.co.uk                              # fetch + print digest + save files
./fetchsite example.co.uk --keep-html                  # also save raw page.html
./fetchsite example.co.uk --force                      # refetch, ignore cache
./fetchsite example.co.uk --json                       # print analysis.json instead of digest
./fetchsite --batch domains.txt --workers 10           # parallel batch + index.csv
./fetchsite --batch rows.txt --workers 15 --out /tmp/out
python3 -m sitefetch example.co.uk                     # same as ./fetchsite
```

Per-site output lands in `<out>/<host>/`:

| File | Contents |
|---|---|
| `digest.md` | LLM-friendly report (fetch info, verdict hints, structure, CTAs, contact, flags, full page content as markdown) |
| `content.md` | Full page content as markdown (headings, lists, links, images, tables; URLs resolved absolute) |
| `analysis.json` | All structured signals (full text lives in `content.md`, not here) |
| `page.html` | Raw HTML - only with `--keep-html` |

Batch mode also writes `<out>/index.csv`: one row per site with success,
status, final_url, error, platforms, modernity hint, and a JSON blob of flags.

## 2. CLI reference

```
opencode-run equivalent:  python3 -m sitefetch TARGET [TARGET ...] [options]

positional:
  targets               one or more domains or full URLs

options:
  --batch FILE          file with one target per line (see 6.2)
  --out DIR             output dir (default ./fetched)
  --workers N           parallel workers for --batch (default 10)
  --timeout S           per-request timeout seconds (default 8)
  --deadline S          max TOTAL seconds per site (default 30)
  --keep-html           save raw page.html
  --force               refetch even if analysis.json exists (cache bypass)
  --json                print result summary (single target)
```

Notes:
- Single-target mode prints the digest to stdout (unless cached; use
  `--force` or read `<out>/<host>/digest.md` directly).
- Batch lines accept plain domains, full URLs, or pasted sheet rows like
  `502 | Company Name | http://www.site.com` - the parser picks the token
  containing the domain and keeps the rest as a label.
- Cache: if `<out>/<host>/analysis.json` exists the site is skipped unless
  `--force`. Progress persists across runs - safe to re-launch failed batches.

## 3. Fetch strategy (fetch.py)

`fetch_site(domain_or_url, timeout=8, deadline=30.0)` tries, in order:

1. **URL candidates**: `http://bare`, `https://bare`, `https://www.bare`,
   `http://www.bare` (bare = input stripped of scheme/www). A full URL input
   is tried first, then its bare variants.
2. Per candidate: one GET with a desktop-Chrome User-Agent.
   - Connection errors / timeouts -> next candidate.
   - HTTP 401/403/406/429/503 -> retry the SAME URL with a Googlebot UA,
     then a curl UA (some hosts whitelist bots).
3. SSL errors -> one retry with `verify=False`, flagged
   `insecure_fallback: true`.
4. Redirects are followed; the response reports the full
   `redirect_chain`, `redirected`, and
   `redirected_to_different_domain` (core-host comparison, www-insensitive).
   That flag is the review-policy "-1: redirects to a different company".
5. Body is capped at ~6 MB. Total time per site is capped by `deadline`
   (default 30 s) - when it expires the site is reported as failed with
   `failure_kind`.

### 3.1 Result dict

```python
{
  "success": bool,
  "entry":   {...} | None,   # winning attempt (see below); no html key
  "html":    str | None,     # decoded HTML on success
  "attempts":[...],          # every attempt: url, ua_kind, status, redirect
                             # chain, timings, or the error
  "error":   str | None,     # last error description
  "failure_kind": None | "blocked" | "unresponsive",
  "variants_tried": int,
}
```

`failure_kind` (only on failure):
- `"blocked"` - last HTTP status was 401/403/406/429/503 (bot-blocking).
  Review policy: ONE WebFetch fallback allowed, then -1.
- `"unresponsive"` - timeouts, connection errors, or the 30 s deadline hit.
  Review policy: -1 immediately, no WebFetch.

Entry fields (success or last attempt): `url`, `ua_kind`, `status`, `ok`,
`final_url`, `redirect_chain` [{status, url}], `redirected`,
`redirected_to_different_domain`, `elapsed_ms`, `server`, `content_type`,
`bytes`, `insecure_fallback`.

## 4. Analysis (analyze.py)

`analyze_html(html, fetch_entry)` parses the HTML (BeautifulSoup + lxml) and
returns a signal dict. `build_digest(host, fetch_result, sig)` renders it to
markdown.

### 4.1 Signal reference (analysis.json keys)

**Page basics**
- `title`, `meta_description`, `meta_generator`
- `viewport_meta` (bool - mobile-friendliness hint), `html_lang`,
  `doctype_html5`, `og_tags` (count of og: meta tags)

**Platform detection** - `platforms` list, matched against raw HTML:
wordpress, elementor, divi, wix, squarespace, shopify, webflow, godaddy,
joomla, drupal, nextjs, nuxt, gatsby, react, vue, angular, jquery (+version),
bootstrap (+version), tailwind, google-sites, framer, duda, ionos, 123-reg,
zoho, bigcommerce, magento, opencart, cpanel-landing. Plus
`jquery_version` / `bootstrap_version` when versioned files are found.

**Structure**
- `h1` {count, texts[:5]}, `h2_count`, `h2_texts[:15]`, `h3_count`
- `nav` {items[:25], count} - anchor texts collected from nav/header/role/
  class*=menu/class*=nav/id*=menu/id*=nav selectors (crowdedness signal)
- `buttons` (button element texts), `cta_links` (anchor texts matching
  CTA words: call/quote/contact/book/buy/get/hire/order/...)
- `links` {total, internal, external}

**Media & contact**
- `images` {total, with_src, with_alt, alt_coverage_pct, lazy, inline_svg}
- `forms` {count, details: [{fields[:10], submit_text}] (first 6 forms)}
- `tel_links`, `mailto_links` (clickable contact = UX positive)
- `contact` {phone_found (UK-pattern regex), emails_found[:4],
  suspicious_emails (test@, example.com, name@company...)}
- `social_links` (facebook/twitter/x/instagram/linkedin/youtube/tiktok/
  pinterest)
- `links` totals

**Content**
- `text` {word_count, content_md (full page as markdown), content_chars}
- `copyright_year` (max year after (c)/©; stale year = unmaintained signal)

**Problem signals** (`signals` dict; lists of matched snippets, [] if none)
- `parked` - domain for sale / parked / hugedomains / sedo / future home of
- `coming_soon` - coming soon / under construction / maintenance / launching
  soon / check back later / pardon our dust ...
- `placeholder` - lorem ipsum, sample text, your company name, john doe,
  123 main street, test@/example emails, [insert, tbd, ...
- `raw_code_leak` - `<?php`, `{{ ... }}`, `[object Object]`,
  `[widget id="..."]`, `[embed]`, PHP warnings, "Notice: Undefined", ...
  (visible template/code leaks = strong broken-site evidence)
- `spam` - casino / viagra / cialis / xxx / betting / slots / payday loan /
  cheap seo ... (hijacked-site indicator)
- `repeated_headings` - identical h1/h2 texts appearing more than once

**Fetch merge** - `fetch` object: the winning entry minus html, plus
`https` bool. On failure: `{success: false, error, failure_kind, attempts}`.

**Heuristic score** - `hints` {modernity_score_0_100, notes[]}. Starts at 50:
+12 viewport, +8 modern platform, +5 https, +8 copyright >= 2024 / -8 <= 2020
(with note), -10 zero images, +4 alt coverage >= 70%, -15 placeholder,
-40 parked, -25 coming soon, -30 spam, -15 raw code leak, -12 word count
< 80 (with note), -5 nav > 14 items (with note), +4 tel/mailto present.
Clamped 0-100. THIS IS A HINT ONLY - the digest must still be read and the
final 1-10 rating is a human/LLM judgment.

### 4.2 Digest format (digest.md)

```
# <host> - fetch digest
## Fetch
- status: 200 in 593ms, final URL: ...
- redirected: yes (to different domain: False)
  - 301 http://efixx.co.uk/
- server: ZGS | bytes: 76396 | insecure_tls: False
(or on failure:)
- FAILED to fetch: <last error>
  - <each attempted URL and its error/status>

## Quick verdict hints
- platforms: ...
- viewport (mobile): yes|NO | https: yes|no | copyright year: ...
- heuristic modernity score: NN/100
  - score notes
- PARKED/COMING SOON/PLACEHOLDER/RAW CODE/SPAM/REPEATED HEADINGS lists

## Page structure
- title, meta description, h1(count): texts, h2(count): texts
- nav items (N): [...]
- images: total, alt coverage %, lazy
- forms: count + fields + submit text (first 3)
- contact: phone, emails, suspicious, tel_links, mailto
- CTA links, buttons, socials, link totals

## Full page content (markdown)
```

## 5. Modules

| File | Role |
|---|---|
| `sitefetch/fetch.py` | `fetch_site()` + `url_variants()`: fallback chains, UA rotation (browser/googlebot/curl), TLS fallback, 30 s deadline, failure classification |
| `sitefetch/analyze.py` | `analyze_html()` signal extraction, `build_digest()` markdown renderer; keyword/pattern tables (PLATFORMS, PARKED, COMING_SOON, PLACEHOLDER, RAW_CODE_LEAKS, SPAM, CTA_WORDS, PHONE/EMAIL/COPYRIGHT regexes, SOCIALS) |
| `sitefetch/cli.py` | `process_one()` (fetch -> analyze -> save -> print) and `run_batch()` (ThreadPool + index.csv); argparse CLI |
| `sitefetch/__main__.py` | enables `python3 -m sitefetch` |
| `fetchsite` | bash wrapper: `cd` to project and exec the module |

### 5.1 Python API

```python
from sitefetch.fetch import fetch_site
from sitefetch.analyze import analyze_html, build_digest

fr = fetch_site("example.co.uk", timeout=8, deadline=30)
if fr["success"]:
    sig = analyze_html(fr["html"], fr["entry"])
    print(build_digest("example.co.uk", fr, sig))
    fr["failure_kind"]        # None
else:
    fr["failure_kind"]        # "blocked" | "unresponsive"
    fr["attempts"]            # full attempt log
```

## 6. Batch mode

Input file example (any mix of these line styles):

```
example.co.uk
https://www.another.com
502 | Company Name | http://www.site.com
543, Other Company, http://www.other.org
# comment lines are ignored
```

Run:
```bash
python3 -m sitefetch --batch rows.txt --workers 15 --out /tmp/opencode/batch
```

- Workers pull targets concurrently; each writes its own `<out>/<host>/`.
- Already-processed sites (analysis.json present) are skipped unless --force.
- Progress lines print as `[i/N] target -> status OK|FAIL reason`.
- `<out>/index.csv` columns: target, label, success, status, final_url,
  error, platforms (JSON string), modernity_hint, signals (JSON string),
  host_dir.
- One broken site never kills the batch; failures are recorded in the CSV.

## 7. Review-policy integration (how the pipeline uses it)

The lead-review workflow (REVIEW-PLAYBOOK.md section 6) instructs every
sub-agent to:

1. Fetch in groups of 3-4 sites per Bash call (keeps each call under the
   command timeout): `cd /root/web2/glm && timeout 100 python3 -m sitefetch
   URL1 URL2 URL3 --out /tmp/opencode/JOB --timeout 8`.
2. Read the printed digest and rate the site's UI + UX per the scoring rules.
3. Failure handling (30 s policy):
   - `failure_kind: unresponsive` (timeout / ConnectionError / deadline) ->
     -1 immediately, no WebFetch.
   - `failure_kind: blocked` (403/401) -> one WebFetch attempt on
     `https://domain` then `http://www.domain`; both fail = -1; returned
     content clearly from a different company = -1.
4. `redirected_to_different_domain: true`, or spam/parked/coming-soon flags,
   are strong evidence for low scores or -1 (verify in the digest text).
5. Shared cache dir per job (`--out /tmp/opencode/JOB`) means duplicate
   domains across agents are fetched once.

## 8. Extending

- New platform: add `(name, [regex, ...])` to `PLATFORMS` in analyze.py.
- New problem signal: add a regex list + a line in `signals` + a digest line
  (see how `raw_code_leak` gained `[widget id="..."]` after it was caught in
  the wild).
- New UA: add to `USER_AGENTS` in fetch.py and reference it in the blocked
  retry loop.
- The heuristic weights live at the bottom of `analyze_html`; tune them only
  as hints - never let the score replace digest judgment.

## 9. Known behaviors / limitations

- Static fetch only: JS-rendered SPA content may be missing (client-side
  text will not appear; nav/buttons may read empty). Judge from what loads -
  a blank static render is itself a UX signal.
- Cookie walls / geo-blocks can return 403 from every UA -> correctly
  classified as blocked (WebFetch fallback decides).
- Some hosts return 200 with a zero-byte or near-empty body - the digest's
  word_count + empty full-content section exposes this (score it as a
  near-empty site).
- Redirect to a rebranded domain of the SAME company is fine; only flag when
  the final content belongs to a different business.
- Very large pages are truncated at ~6 MB; the full-content section then
  covers the top of the page.
