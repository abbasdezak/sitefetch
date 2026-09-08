"""CLI: fetch one site or a batch, save analysis files, print LLM-friendly digest.

Usage:
  python3 -m sitefetch example.com
  python3 -m sitefetch https://www.example.co.uk --keep-html --out fetched
  python3 -m sitefetch --batch domains.txt --workers 10
  python3 -m sitefetch example.com --json        # print analysis.json instead
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import re
import sys
from pathlib import Path

from .fetch import fetch_site
from .analyze import analyze_html, build_digest


def _safe_name(host_or_url: str) -> str:
    name = re.sub(r"^https?://", "", host_or_url.strip().lower())
    name = name.split("/")[0]
    return re.sub(r"[^a-z0-9.-]", "_", name) or "site"


def _parse_batch_line(line: str):
    """Accept 'domain', 'url', or rows like '502 | Name | http://x' / '502,Name,x'.
    Returns (url_or_domain, label)."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None, None
    parts = [p.strip() for p in re.split(r"[|,\t]", raw) if p.strip()]
    if len(parts) == 1:
        return parts[0], parts[0]
    for p in parts:
        if "." in p and " " not in p:
            label = " ".join(x for x in parts if x != p)
            return p, label or p
    return parts[-1], " ".join(parts[:-1]) or parts[-1]


def process_one(target: str, out_dir: Path, keep_html: bool = False,
                timeout: int = 8, force: bool = False, quiet: bool = False,
                deadline: float = 30.0) -> dict:
    name = _safe_name(target)
    site_dir = out_dir / name
    analysis_path = site_dir / "analysis.json"
    if analysis_path.exists() and not force:
        result = json.loads(analysis_path.read_text())
        result["cached"] = True
        return result

    fr = fetch_site(target, timeout=timeout, deadline=deadline)
    if fr["success"]:
        sig = analyze_html(fr["html"], fr["entry"])
    else:
        sig = analyze_html("<html><body></body></html>", None)
        sig["fetch"] = {"success": False, "error": fr["error"],
                        "failure_kind": fr.get("failure_kind"),
                        "attempts": fr["attempts"]}

    digest = build_digest(_safe_name(target), fr, sig)

    site_dir.mkdir(parents=True, exist_ok=True)
    sig_json = {k: v for k, v in sig.items() if k != "text"}
    sig_json["text"] = {k: v for k, v in (sig.get("text") or {}).items()
                        if k != "content_md"}
    (site_dir / "analysis.json").write_text(json.dumps(sig_json, indent=1))
    (site_dir / "digest.md").write_text(digest)
    if (sig.get("text") or {}).get("content_md"):
        (site_dir / "content.md").write_text(sig["text"]["content_md"])
    if keep_html and fr.get("html"):
        (site_dir / "page.html").write_text(fr["html"])

    result = {
        "target": target,
        "host_dir": str(site_dir),
        "success": fr["success"],
        "status": (fr.get("entry") or {}).get("status"),
        "final_url": (fr.get("entry") or {}).get("final_url"),
        "error": fr.get("error"),
        "platforms": sig.get("platforms"),
        "modernity_hint": (sig.get("hints") or {}).get("modernity_score_0_100"),
        "signals": {k: v for k, v in (sig.get("signals") or {}).items() if v},
    }
    if not quiet:
        print(digest)
        print(f"\n[files] {site_dir}/(digest.md, analysis.json, content.md"
              f"{', page.html' if keep_html else ''})")
    return result


def run_batch(batch_file: Path, out_dir: Path, workers: int, keep_html: bool,
              timeout: int, force: bool, deadline: float = 30.0) -> None:
    targets, labels = [], []
    for line in batch_file.read_text().splitlines():
        t, label = _parse_batch_line(line)
        if t:
            targets.append(t)
            labels.append(label)
    print(f"batch: {len(targets)} targets, {workers} workers")

    rows = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_one, t, out_dir, keep_html, timeout, force,
                          True, deadline): (t, label)
                for t, label in zip(targets, labels)}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            t, label = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # never let one site kill the batch
                r = {"target": t, "success": False, "error": f"internal: {e!r}"}
            r["label"] = label
            rows.append(r)
            status = r.get("status") or ("ERR" if not r.get("success") else "?")
            print(f"[{i}/{len(targets)}] {t} -> {status} "
                  f"{'OK' if r.get('success') else 'FAIL ' + str(r.get('error'))[:60]}")

    csv_path = out_dir / "index.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "target", "label", "success", "status", "final_url", "error",
            "platforms", "modernity_hint", "signals", "host_dir"])
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(r.get(k)) if k in ("platforms", "signals") else r.get(k))
                        for k in w.fieldnames})
    print(f"index: {csv_path}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sitefetch",
                                 description="Best-effort website fetcher + LLM-friendly analyzer")
    ap.add_argument("targets", nargs="*", help="domain or URL(s)")
    ap.add_argument("--batch", type=Path, help="file with one domain/URL per line")
    ap.add_argument("--out", type=Path, default=Path("fetched"), help="output dir (default ./fetched)")
    ap.add_argument("--workers", type=int, default=10, help="parallel workers for --batch")
    ap.add_argument("--timeout", type=int, default=8, help="per-request timeout seconds")
    ap.add_argument("--deadline", type=float, default=30.0,
                    help="max total seconds per site (30s policy: unresponsive = -1)")
    ap.add_argument("--keep-html", action="store_true", help="save raw page.html")
    ap.add_argument("--force", action="store_true", help="refetch even if cached")
    ap.add_argument("--json", action="store_true", help="print analysis.json (single target)")
    args = ap.parse_args(argv)

    if args.batch:
        run_batch(args.batch, args.out, args.workers, args.keep_html, args.timeout,
                  args.force, args.deadline)
        return
    if not args.targets:
        ap.error("give a domain/URL or --batch FILE")

    for t in args.targets:
        result = process_one(t, args.out, keep_html=args.keep_html,
                             timeout=args.timeout, force=args.force,
                             deadline=args.deadline, quiet=args.json)
        if args.json:
            print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
