"""HTML -> structured signals + LLM-friendly digest.

Everything an LLM needs to judge a site's UI/UX quality without a browser:
platform, structure, content density, contact/CTA discoverability, placeholder
and spam signals, datedness, plus a heuristic modernity score (a hint only).
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ---------------------------------------------------------------- keyword sets
PLATFORMS = [
    ("wordpress",   [r"wp-content", r"wp-includes", r"/wordpress"]),
    ("elementor",   [r"elementor"]),
    ("divi",        [r"themes/Divi", r"et_builder"]),
    ("wix",         [r"wix\.com", r"wixstatic", r"_wix"]),
    ("squarespace", [r"squarespace"]),
    ("shopify",     [r"shopify", r"cdn\.shopify"]),
    ("webflow",     [r"webflow"]),
    ("godaddy",     [r"godaddy", r"starfield"]),
    ("joomla",      [r"joomla"]),
    ("drupal",      [r"drupal"]),
    ("wixstudio",   [r"wixstudio"]),
    ("nextjs",      [r"/_next/"]),
    ("nuxt",        [r"/_nuxt/"]),
    ("gatsby",      [r"gatsby"]),
    ("react",       [r"data-reactroot", r"react(?:\.production)?\.min\.js", r"_react"]),
    ("vue",         [r"data-v- app", r"vue(?:\.runtime)?(?:\.min)?\.js"]),
    ("angular",     [r"ng-version", r"angular"]),
    ("jquery",      [r"jquery[-.]?([\d.]+)?\.js"]),
    ("bootstrap",   [r"bootstrap(?:\.min)?(?:\.[\d.]+)?\.(?:css|js)"]),
    ("tailwind",    [r"tailwind"]),
    ("google-sites",[r"sites\.google\.com"]),
    ("framer",      [r"framer"]),
    ("duda",        [r"duda"]),
    ("ionos",       [r"ionos", r"1and1"]),
    ("123-reg",     [r"123-reg"]),
    ("zoho",        [r"zoho"]),
    ("bigcommerce", [r"bigcommerce"]),
    ("magento",     [r"magento"]),
    ("opencart",    [r"opencart"]),
    ("cpanel-landing", [r"cpanel"]),
]

PARKED_PATTERNS = [
    r"domain\s+(?:is\s+)?for\s+sale", r"buy\s+this\s+domain", r"parked\s+domain",
    r"this\s+website\s+is\s+for\s+sale", r"future\s+home\s+of", r"hugedomains",
    r"\bsedo\b", r"afternic", r"is\s+parked",
]

COMING_SOON = [
    r"coming\s+soon", r"under\s+construction", r"under\s+maintenance",
    r"launching\s+soon", r"website\s+launching", r"new\s+site\s+coming",
    r"opening\s+soon", r"opening\s+early", r"site\s+is\s+offline",
    r"maintenance\s+mode", r"pardon\s+our\s+dust", r"check\s+back\s+(?:soon|later)",
]

PLACEHOLDER_PATTERNS = [
    r"lorem\s+ipsum", r"sample\s+text", r"placeholder\s+text", r"your\s+company\s+name",
    r"your\s+name\s+here", r"company\s+name\s+here", r"insert\s+(?:text|here|your)",
    r"john\s+doe", r"jane\s+doe", r"123\s+main\s+street", r"123\s+main\s+st",
    r"test@test\.", r"@example\.com", r"example\.org", r"name@company",
    r"your\s+email\s+here", r"phone\s+number\s+here", r"address\s+here",
    r"tbd\b", r"to\s+be\s+confirmed", r"\[insert", r"\[your\s",
    r"info@test\.", r"email@email\.", r"youremail@", r"yourwebsite\.",
]

RAW_CODE_LEAKS = [
    r"<\?php", r"<%=", r"\{\{.*?\}\}", r"\{%.*?%\}", r"\[object\s+Object\]",
    r"\[widget id=\"?[^\]]+\]", r"\[embed\]", r"\[caption[^\]]*\]",
    r"stdClass\s*Object", r"Fatal\s+error", r"Warning:\s+", r"Parse\s+error",
    r"Notice:\s+Undefined", r"function\s*\(\)\s*\{", r"var\s+\w+\s*=.*;;",
    r"e\.g\.\s*\[", r"Array\s*\(",
]

SPAM_PATTERNS = [
    r"\bcasino\b", r"\bviagra\b", r"\bcialis\b", r"\bporn\b", r"\bxxx\b",
    r"\bpharmacy\b", r"\bbetting\b", r"\bslots\b", r"\bpayday\s+loan",
    r"\b escorts \b", r"\bforex\s+signals\b", r"\bpenis\b", r"\b-pills\b",
    r"cheap\s+seo", r"\bbacklinks\s+cheap",
]

CTA_WORDS = re.compile(
    r"\b(call|phone|quote|contact|enquir|enquir|book|buy|get\s+a|get\s+your|hire|"
    r"free|order|shop|visit|email\s+us|find\s+out|learn\s+more|request)\b", re.I)

PHONE_RE = re.compile(r"(?:\+44|0)\d[\d\s().-]{8,}\d")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")
COPYRIGHT_RE = re.compile(r"(?:©|\(c\)|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(20\d{2}|19\d{2})", re.I)

SOCIALS = ["facebook", "twitter", "x.com", "instagram", "linkedin", "youtube",
           "tiktok", "pinterest"]


def _clean_text(soup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _texts_of(nodes):
    out = []
    for n in nodes:
        t = n.get_text(" ", strip=True)
        if t:
            out.append(t)
    return out


# ------------------------------------------------------------- full content --
_MD_SKIP = {"script", "style", "noscript", "template", "svg", "head", "meta",
            "link", "select", "option", "datalist", "input"}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


def _inline_md(node, base_url: str) -> str:
    """Render a node as inline markdown (links, images, bold/italic kept)."""
    if isinstance(node, NavigableString):
        return "" if isinstance(node, Comment) else str(node)
    name = (node.name or "").lower()
    if name in _MD_SKIP:
        return ""
    if name == "br":
        return "\n"
    if name == "a":
        inner = re.sub(r"\s+", " ", "".join(
            _inline_md(c, base_url) for c in node.children)).strip()
        href = (node.get("href") or "").strip()
        if href and not href.startswith(("#", "javascript:", "data:")) and inner:
            return f"[{inner}]({urljoin(base_url, href)})"
        return inner
    if name == "img":
        src = (node.get("src") or node.get("data-src") or "").strip()
        alt = (node.get("alt") or "").strip()
        if src and not src.startswith("data:"):
            return f"![{alt}]({urljoin(base_url, src)})"
        return alt
    if name in ("strong", "b", "em", "i"):
        inner = "".join(_inline_md(c, base_url) for c in node.children).strip()
        if not inner:
            return ""
        return f"**{inner}**" if name in ("strong", "b") else f"*{inner}*"
    return "".join(_inline_md(c, base_url) for c in node.children)


def _emit_list(node, out: list, base_url: str, indent: str = "") -> None:
    ordered = (node.name or "").lower() == "ol"
    items = node.find_all("li", recursive=False) or node.find_all("li")
    for i, li in enumerate(items, 1):
        marker = f"{indent}{i}." if ordered else f"{indent}-"
        parts, sublists = [], []
        for c in li.children:
            if isinstance(c, Tag) and (c.name or "").lower() in ("ul", "ol"):
                sublists.append(c)
            else:
                parts.append(_inline_md(c, base_url))
        t = re.sub(r"\s+", " ", "".join(parts)).strip()
        if t:
            out.append(f"{marker} {t}")
        for sl in sublists:
            _emit_list(sl, out, base_url, indent + "  ")


def _blocks_md(node, out: list, base_url: str) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if not isinstance(child, Comment):
                t = re.sub(r"\s+", " ", str(child)).strip()
                if t:
                    out.append(t)
            continue
        name = (child.name or "").lower()
        if name in _MD_SKIP:
            continue
        if name in _HEADINGS:
            t = _inline_md(child, base_url).strip()
            if t:
                out.append("#" * _HEADINGS[name] + " " + t)
        elif name == "p":
            t = _inline_md(child, base_url).strip()
            if t:
                out.append(t)
        elif name in ("ul", "ol"):
            sub: list[str] = []
            _emit_list(child, sub, base_url)
            if sub:
                out.append("\n".join(sub))
        elif name == "li":
            t = _inline_md(child, base_url).strip()
            if t:
                out.append(f"- {t}")
        elif name == "blockquote":
            t = _inline_md(child, base_url).strip()
            if t:
                out.extend("> " + ln for ln in t.splitlines())
        elif name == "pre":
            t = child.get_text().strip()
            if t:
                out.append(f"```\n{t}\n```")
        elif name == "table":
            lines = []
            for tr in child.find_all("tr"):
                cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True))
                         for c in tr.find_all(["th", "td"])]
                if cells:
                    lines.append("| " + " | ".join(cells) + " |")
            if lines:
                out.append("\n".join(lines))
        elif name == "hr":
            out.append("---")
        elif name == "img":
            t = _inline_md(child, base_url).strip()
            if t:
                out.append(t)
        elif name == "a":
            t = _inline_md(child, base_url).strip()
            if t:
                out.append(t)
        elif name == "source":
            pass  # emitted via parent video/audio
        elif name in ("iframe", "video", "audio", "embed"):
            src = (child.get("src") or "").strip()
            if not src:
                s = child.find("source", src=True) or child.find("a", href=True)
                src = ((s.get("src") or s.get("href") or "").strip()) if s else ""
            if src:
                out.append(f"[{name}]({urljoin(base_url, src)})")
        else:
            _blocks_md(child, out, base_url)


def html_to_markdown(html: str, base_url: str = "") -> str:
    """Full-page HTML -> structured markdown (headings, lists, links, images,
    tables). URLs resolved against base_url. Nothing visible is dropped."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    body = soup.body or soup
    out: list[str] = []
    _blocks_md(body, out, base_url)
    lines = []
    for ln in out:
        ln = re.sub(r"[ \t]+", " ", ln).strip()
        if ln and (not lines or ln != lines[-1]):  # collapse adjacent repeats
            lines.append(ln)
    return "\n\n".join(lines)


def analyze_html(html: str, fetch_entry: dict | None = None) -> dict:
    soup = BeautifulSoup(html, "lxml")
    low = html.lower()

    sig: dict = {}

    # ---------------- basics -------------------------------------------------
    title_tag = soup.find("title")
    sig["title"] = title_tag.get_text(" ", strip=True) if title_tag else ""
    md = soup.find("meta", attrs={"name": "description"})
    sig["meta_description"] = (md.get("content", "").strip() if md and md.get("content") else "")
    gen = soup.find("meta", attrs={"name": "generator"})
    sig["meta_generator"] = (gen.get("content", "").strip() if gen and gen.get("content") else "")
    vp = soup.find("meta", attrs={"name": "viewport"})
    sig["viewport_meta"] = bool(vp)
    sig["html_lang"] = (soup.html.get("lang", "") if soup.html else "")
    sig["doctype_html5"] = bool(re.search(r"<!doctype\s+html>", html[:200], re.I))
    sig["og_tags"] = len(soup.find_all("meta", attrs={"property": re.compile(r"^og:")}))

    # ---------------- platform detection ------------------------------------
    found = []
    for name, pats in PLATFORMS:
        for p in pats:
            if re.search(p, low):
                found.append(name)
                break
    jq = re.search(r"jquery[-.]?([\d]+\.[\d]+[^/\s\"']*)?\.js", low)
    boot = re.search(r"bootstrap(?:\.min)?(?:[-.]?([\d]+\.[\d]+)[^/\s\"']*)?\.(?:css|js)", low)
    sig["platforms"] = found
    if jq:
        sig["jquery_version"] = jq.group(1) or "unknown"
    if boot:
        sig["bootstrap_version"] = boot.group(1) or "unknown"

    # ---------------- structure ---------------------------------------------
    h1s = _texts_of(soup.find_all("h1"))
    h2s = _texts_of(soup.find_all("h2"))
    h3s = _texts_of(soup.find_all("h3"))
    sig["h1"] = {"count": len(h1s), "texts": h1s[:5]}
    sig["h2_count"] = len(h2s)
    sig["h2_texts"] = h2s[:15]
    sig["h3_count"] = len(h3s)

    nav_nodes = soup.select(
        "nav a, header a, [role=navigation] a, [class*=menu] a, [id*=menu] a, "
        "[class*=nav] a, [id*=nav] a, ul[class*=menu] li a")
    nav_texts = []
    for a in nav_nodes[:25]:
        t = a.get_text(" ", strip=True)
        if t and len(t) < 40 and t not in nav_texts:
            nav_texts.append(t)
    sig["nav"] = {"items": nav_texts, "count": len(nav_texts)}

    # buttons & CTA-ish links
    btn_texts = []
    for b in soup.find_all("button")[:20]:
        t = b.get_text(" ", strip=True)
        if t:
            btn_texts.append(t[:60])
    cta_links = []
    for a in soup.find_all("a", href=True):
        t = a.get_text(" ", strip=True)
        if t and CTA_WORDS.search(t) and len(t) < 60 and t not in cta_links:
            cta_links.append(t)
    sig["buttons"] = btn_texts[:12]
    sig["cta_links"] = cta_links[:12]

    # links
    hrefs = [a.get("href", "") for a in soup.find_all("a", href=True)]
    internal = sum(1 for h in hrefs if h.startswith(("/", "#")) or "mailto:" in h or "tel:" in h)
    external = sum(1 for h in hrefs if h.startswith("http"))
    sig["links"] = {"total": len(hrefs), "internal": internal, "external": external}

    # socials
    socials = sorted({s for s in SOCIALS if s in low})
    sig["social_links"] = socials

    # tel / mailto
    sig["tel_links"] = sum(1 for h in hrefs if h.lower().startswith("tel:"))
    sig["mailto_links"] = sum(1 for h in hrefs if h.lower().startswith("mailto:"))

    # ---------------- images -------------------------------------------------
    imgs = soup.find_all("img")
    with_src = [i for i in imgs if i.get("src") and not i.get("src").startswith("data:")]
    with_alt = [i for i in with_src if (i.get("alt") or "").strip()]
    lazy = [i for i in with_src if i.get("loading") == "lazy"]
    svg_imgs = len(soup.find_all("svg"))
    sig["images"] = {
        "total": len(imgs),
        "with_src": len(with_src),
        "with_alt": len(with_alt),
        "alt_coverage_pct": round(100 * len(with_alt) / len(with_src)) if with_src else None,
        "lazy": len(lazy),
        "inline_svg": svg_imgs,
    }

    # ---------------- forms & contact ---------------------------------------
    forms = []
    for f in soup.find_all("form")[:6]:
        fields = []
        for inp in f.find_all(["input", "select", "textarea"]):
            name = inp.get("name") or inp.get("placeholder") or inp.get("type") or ""
            if name:
                fields.append(str(name)[:30])
        submit = f.find(["button", "input"], attrs={"type": "submit"})
        forms.append({
            "fields": fields[:10],
            "submit_text": submit.get("value") or submit.get_text(" ", strip=True) if submit else "",
        })
    sig["forms"] = {"count": len(soup.find_all("form")), "details": forms}

    # ---------------- text ---------------------------------------------------
    text = _clean_text(soup)
    words = text.split()
    base_url = (fetch_entry or {}).get("final_url") or ""
    content_md = html_to_markdown(html, base_url)
    sig["text"] = {
        "word_count": len(words),
        "preview": text[:1200],
        "footer_tail": text[-400:],
        "content_md": content_md,
        "content_chars": len(content_md),
    }

    phone_match = PHONE_RE.search(text)
    email_matches = EMAIL_RE.findall(text)
    bad_emails = [e for e in email_matches if re.search(
        r"test|example|@email\.|youremail|name@company|domain\.com|email\.com", e, re.I)]
    sig["contact"] = {
        "phone_found": phone_match.group(0).strip() if phone_match else None,
        "emails_found": list(dict.fromkeys(email_matches))[:4],
        "suspicious_emails": list(dict.fromkeys(bad_emails))[:4],
    }

    years = [int(y) for y in COPYRIGHT_RE.findall(text)]
    sig["copyright_year"] = max(years) if years else None

    # ---------------- problem signals ---------------------------------------
    def hits(pats):
        out = []
        for p in pats:
            m = re.search(p, text, re.I)
            if m:
                out.append(m.group(0)[:40])
        return out

    sig["signals"] = {
        "parked": hits(PARKED_PATTERNS),
        "coming_soon": hits(COMING_SOON),
        "placeholder": hits(PLACEHOLDER_PATTERNS),
        "raw_code_leak": hits(RAW_CODE_LEAKS),
        "spam": hits(SPAM_PATTERNS),
    }

    dup_headings = [t for t, c in Counter([x.lower() for x in h1s + h2s]).items() if c > 1]
    sig["signals"]["repeated_headings"] = dup_headings[:5]

    # ---------------- fetch-side info merged --------------------------------
    if fetch_entry:
        sig["fetch"] = {k: v for k, v in fetch_entry.items() if k != "html"}
        sig["fetch"]["https"] = fetch_entry.get("final_url", "").startswith("https")

    # ---------------- heuristic score (hint only) ---------------------------
    score, notes = 50, []
    if sig["viewport_meta"]:
        score += 12
    else:
        notes.append("no viewport meta (likely not mobile friendly)")
    if any(p in found for p in ("wordpress", "wix", "squarespace", "shopify", "webflow",
                                "nextjs", "nuxt", "react", "elementor")):
        score += 8
    if fetch_entry and fetch_entry.get("final_url", "").startswith("https"):
        score += 5
    cy = sig["copyright_year"]
    if cy:
        if cy >= 2024:
            score += 8
        elif cy <= 2020:
            score -= 8
            notes.append(f"copyright stuck at {cy} (site looks unmaintained)")
    ic = sig["images"]["total"]
    if ic == 0:
        score -= 10
        notes.append("no images at all")
    elif sig["images"]["alt_coverage_pct"] is not None and sig["images"]["alt_coverage_pct"] >= 70:
        score += 4
    if sig["signals"]["placeholder"]:
        score -= 15
    if sig["signals"]["parked"]:
        score -= 40
    if sig["signals"]["coming_soon"]:
        score -= 25
    if sig["signals"]["spam"]:
        score -= 30
    if sig["signals"]["raw_code_leak"]:
        score -= 15
    wc = sig["text"]["word_count"]
    if wc < 80:
        score -= 12
        notes.append(f"very little text ({wc} words)")
    if len(sig["nav"]["items"]) > 14:
        score -= 5
        notes.append(f"crowded nav ({len(sig['nav']['items'])} items)")
    if sig["tel_links"] or sig["mailto_links"]:
        score += 4
    sig["hints"] = {"modernity_score_0_100": max(0, min(100, score)), "notes": notes}
    return sig


# ------------------------------------------------------------------- digest --
def build_digest(host: str, fetch_result: dict, sig: dict) -> str:
    e = fetch_result.get("entry") or {}
    L = []
    L.append(f"# {host} - fetch digest\n")
    L.append("## Fetch")
    if fetch_result.get("success"):
        L.append(f"- status: {e.get('status')} in {e.get('elapsed_ms')}ms, final URL: {e.get('final_url')}")
        if e.get("redirected"):
            L.append(f"- redirected: yes (to different domain: {e.get('redirected_to_different_domain')})")
            for h in e.get("redirect_chain", []):
                L.append(f"  - {h.get('status')} {h.get('url')}")
        L.append(f"- server: {e.get('server') or 'n/a'} | bytes: {e.get('bytes')} | insecure_tls: {e.get('insecure_fallback', False)}")
    else:
        L.append(f"- FAILED to fetch: {fetch_result.get('error')}")
        for a in fetch_result.get("attempts", []):
            if a.get("error"):
                L.append(f"  - {a['url']}: {a['error']}")
            else:
                L.append(f"  - {a['url']}: HTTP {a.get('status')} (ua={a.get('ua_kind')})")
    L.append("")

    L.append("## Quick verdict hints")
    plat = ", ".join(sig.get("platforms", [])) or "unknown"
    L.append(f"- platforms: {plat}")
    if sig.get("meta_generator"):
        L.append(f"- generator meta: {sig['meta_generator']}")
    L.append(f"- viewport (mobile): {'yes' if sig.get('viewport_meta') else 'NO'} | "
             f"https: {'yes' if (sig.get('fetch') or {}).get('https') else 'no'} | "
             f"copyright year: {sig.get('copyright_year') or 'not found'}")
    h = sig.get("hints", {})
    L.append(f"- heuristic modernity score: {h.get('modernity_score_0_100')}/100")
    for n in h.get("notes", []):
        L.append(f"  - {n}")
    s = sig.get("signals", {})
    for key, label in (("parked", "PARKED/FOR-SALE"), ("coming_soon", "COMING SOON"),
                       ("placeholder", "PLACEHOLDER TEXT"), ("raw_code_leak", "RAW CODE LEAKED"),
                       ("spam", "SPAM CONTENT"), ("repeated_headings", "REPEATED HEADINGS")):
        if s.get(key):
            L.append(f"- {label}: {s[key]}")
    L.append("")

    L.append("## Page structure")
    L.append(f"- title: {sig.get('title') or '(none)'}")
    if sig.get("meta_description"):
        L.append(f"- meta description: {sig['meta_description'][:200]}")
    h1 = sig.get("h1", {})
    L.append(f"- h1 ({h1.get('count')}): {h1.get('texts')}")
    L.append(f"- h2 ({sig.get('h2_count')}): {sig.get('h2_texts')}")
    nav = sig.get("nav", {})
    L.append(f"- nav items ({nav.get('count')}): {nav.get('items')}")
    im = sig.get("images", {})
    L.append(f"- images: {im.get('total')} total, alt coverage {im.get('alt_coverage_pct')}%, lazy {im.get('lazy')}")
    fm = sig.get("forms", {})
    L.append(f"- forms: {fm.get('count')}")
    for f in fm.get("details", [])[:3]:
        L.append(f"  - fields: {f['fields']} submit: {f['submit_text']!r}")
    c = sig.get("contact", {})
    L.append(f"- contact: phone={c.get('phone_found')} emails={c.get('emails_found')} "
             f"suspicious={c.get('suspicious_emails')} tel_links={sig.get('tel_links')} mailto={sig.get('mailto_links')}")
    L.append(f"- CTA links: {sig.get('cta_links')}")
    L.append(f"- buttons: {sig.get('buttons')}")
    L.append(f"- socials: {sig.get('social_links')}")
    L.append(f"- links: {sig.get('links')}")
    L.append("")

    L.append("## Full page content (markdown)")
    L.append(sig.get("text", {}).get("content_md") or "(no text)")
    return "\n".join(L)
