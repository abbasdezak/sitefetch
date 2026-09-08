"""Robust website fetching with URL fallback chains, UA rotation and diagnostics.

Strategy (tuned for the site-review workflow):
  1. Build candidate URLs: http://domain -> https://domain -> https://www.domain
     -> http://www.domain (bare domain = input minus any scheme/www).
  2. Per candidate: one browser-UA GET. Connection errors / timeouts move on to
     the next candidate. HTTP 403/406/429/503 retries the same URL with a
     Googlebot UA, then a curl UA (some hosts whitelist bots).
  3. On SSL errors, retry once with verify=False and flag it.
  4. Follow redirects; report the full chain and whether we ended up on a
     different core domain (the "redirects to a different company" signal).
  5. Cap body size; return raw HTML + a full attempt log.
"""
from __future__ import annotations

import time
import urllib.parse

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

USER_AGENTS = {
    "browser": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "googlebot": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "curl": "curl/8.5.0",
}

MAX_BYTES = 6_000_000


def _host(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _core(host: str) -> str:
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def url_variants(domain_or_url: str) -> list[str]:
    s = (domain_or_url or "").strip()
    if not s:
        return []
    if "://" in s:
        host = _host(s)
        bare = _core(host)
    else:
        bare = s.lower()
        if bare.startswith("www."):
            bare = bare[4:]
    cands = [
        f"http://{bare}",
        f"https://{bare}",
        f"https://www.{bare}",
        f"http://www.{bare}",
    ]
    seen, out = set(), []
    for u in cands:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _get(url, ua_kind, timeout, verify=True):
    t0 = time.time()
    resp = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENTS[ua_kind],
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-GB,en;q=0.9",
            "Connection": "close",
        },
        timeout=timeout,
        allow_redirects=True,
        verify=verify,
        stream=True,
    )
    content = b""
    for chunk in resp.iter_content(chunk_size=65536):
        content += chunk
        if len(content) > MAX_BYTES:
            break
    return resp, content, int((time.time() - t0) * 1000)


def _mk_entry(url, ua_kind, resp, content, elapsed_ms, insecure=False):
    final = resp.url
    chain = [{"status": h.status_code, "url": h.url} for h in resp.history]
    entry = {
        "url": url,
        "ua_kind": ua_kind,
        "status": resp.status_code,
        "ok": 200 <= resp.status_code < 300,
        "final_url": final,
        "redirect_chain": chain,
        "redirected": bool(chain),
        "redirected_to_different_domain": (
            bool(chain) and _core(_host(final)) != _core(_host(url))
        ),
        "elapsed_ms": elapsed_ms,
        "server": resp.headers.get("server", ""),
        "content_type": resp.headers.get("content-type", ""),
        "bytes": len(content),
        "insecure_fallback": insecure,
    }
    return entry, content


def _decode(content: bytes, encoding: str) -> str:
    try:
        return content.decode(encoding or "utf-8", errors="replace")
    except Exception:
        return content.decode("utf-8", errors="replace")


def fetch_site(domain_or_url: str, timeout: int = 8, max_bytes: int = MAX_BYTES,
               deadline: float = 30.0) -> dict:
    """Fetch a site as best as possible.

    deadline: max TOTAL seconds spent trying this site (default 30). Past the
    deadline we stop and report failure_kind='unresponsive' (-1 per policy).
    Returns:
       {success, entry, html, attempts, error, failure_kind}
       failure_kind: None on success, else 'blocked' (HTTP 401/403/406/429/503),
       'unresponsive' (timeouts/connection errors/deadline) or 'bad_status'.
    """
    attempts: list[dict] = []
    last_err = None
    last_status = None
    t_start = time.time()
    tried_variants = 0

    def out_of_time():
        return (time.time() - t_start) >= deadline

    for url in url_variants(domain_or_url):
        if out_of_time():
            last_err = f"deadline {deadline:.0f}s exceeded before {url}"
            break
        tried_variants += 1
        # --- attempt 1: browser UA, verified TLS -----------------------------
        try:
            resp, content, ms = _get(url, "browser", timeout)
        except requests.exceptions.SSLError:
            if out_of_time():
                last_err = f"deadline exceeded at {url} (ssl)"
                break
            # --- attempt 1b: same URL, TLS verification off ------------------
            try:
                resp, content, ms = _get(url, "browser", timeout, verify=False)
            except requests.exceptions.Timeout:
                attempts.append({"url": url, "error": "timeout"})
                last_err = f"timeout on {url}"
                continue
            except requests.exceptions.RequestException as e:
                attempts.append({"url": url, "error": e.__class__.__name__})
                last_err = f"{e.__class__.__name__} on {url}"
                continue
            entry, _ = _mk_entry(url, "browser", resp, content, ms, insecure=True)
            attempts.append({k: v for k, v in entry.items() if k != "html"})
            if entry["ok"] and "html" in entry["content_type"].lower():
                entry["html"] = _decode(content, entry["encoding"] if "encoding" in entry else "utf-8")
                return {"success": True, "entry": entry, "html": entry["html"],
                        "attempts": attempts, "error": None, "failure_kind": None}
            last_err = f"HTTP {entry['status']} (insecure) on {url}"
            last_status = entry["status"]
            continue
        except requests.exceptions.Timeout:
            attempts.append({"url": url, "error": "timeout"})
            last_err = f"timeout on {url}"
            continue
        except requests.exceptions.RequestException as e:
            attempts.append({"url": url, "error": e.__class__.__name__})
            last_err = f"{e.__class__.__name__} on {url}"
            continue

        entry, _ = _mk_entry(url, "browser", resp, content, ms)
        attempts.append({k: v for k, v in entry.items() if k != "html"})

        if entry["ok"] and "html" in entry["content_type"].lower():
            entry["html"] = _decode(content, entry["encoding"] if "encoding" in entry else "utf-8")
            return {"success": True, "entry": entry, "html": entry["html"],
                    "attempts": attempts, "error": None, "failure_kind": None}

        last_status = entry["status"]

        # --- blocked? try bot UAs on the same URL (within deadline) ----------
        if entry["status"] in (401, 403, 406, 429, 503):
            for bot in ("googlebot", "curl"):
                if out_of_time():
                    break
                try:
                    resp, content, ms = _get(url, bot, timeout)
                except requests.exceptions.SSLError:
                    try:
                        resp, content, ms = _get(url, bot, timeout, verify=False)
                    except Exception as e:
                        attempts.append({"url": url, "ua_kind": bot,
                                         "error": e.__class__.__name__})
                        continue
                except Exception as e:
                    attempts.append({"url": url, "ua_kind": bot,
                                     "error": e.__class__.__name__})
                    continue
                entry, _ = _mk_entry(url, bot, resp, content, ms)
                attempts.append({k: v for k, v in entry.items() if k != "html"})
                if entry["ok"] and "html" in entry["content_type"].lower():
                    entry["html"] = _decode(content, entry["encoding"] if "encoding" in entry else "utf-8")
                    return {"success": True, "entry": entry, "html": entry["html"],
                            "attempts": attempts, "error": None, "failure_kind": None}
                last_err = f"HTTP {entry['status']} on {url} (ua={bot})"
                last_status = entry["status"]
        else:
            last_err = f"HTTP {entry['status']} on {url}"

    blocked = last_status in (401, 403, 406, 429, 503)
    kind = "blocked" if blocked else "unresponsive"
    return {"success": False, "entry": None, "html": None,
            "attempts": attempts, "error": last_err or "all attempts failed",
            "variants_tried": tried_variants, "failure_kind": kind}
