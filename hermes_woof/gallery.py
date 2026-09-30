"""Static gallery page for one WOOF run (standard library only).

Written beside the run as gallery.html. Pictures are referenced by relative
path (media/...), so the folder can be copied or zipped and still opens in any
browser. One card per product with a frame slider.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WOOF run {run_id}</title>
<style>
:root {{ --bg:#f6f7f9; --fg:#14171c; --muted:#5b6472; --card:#ffffff; --line:#dde1e7; --accent:#1f6feb; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1216; --fg:#e7eaee; --muted:#9aa4b2; --card:#171b21; --line:#2a313b; --accent:#58a6ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
header {{ padding:20px 16px 8px; max-width:1200px; margin:0 auto; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.meta {{ color:var(--muted); font-size:13px; display:flex; flex-wrap:wrap; gap:6px 16px; }}
main {{ max-width:1200px; margin:0 auto; padding:8px 16px 40px; display:grid; gap:16px;
       grid-template-columns:repeat(auto-fill,minmax(min(100%,520px),1fr)); }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
.card h2 {{ font-size:14px; margin:0; padding:10px 12px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; gap:8px; }}
.card h2 span {{ color:var(--muted); font-weight:400; }}
.card img {{ width:100%; height:auto; display:block; background:#fff; }}
.ctl {{ display:flex; align-items:center; gap:10px; padding:8px 12px; }}
.ctl input {{ flex:1; accent-color:var(--accent); }}
.ctl output {{ font-variant-numeric:tabular-nums; color:var(--muted); font-size:13px; min-width:9.5em; text-align:right; }}
footer {{ color:var(--muted); font-size:12px; text-align:center; padding:0 16px 24px; }}
</style></head>
<body>
<header><h1>WOOF run {run_id}</h1><div class="meta">{meta}</div></header>
<main id="g"></main>
<footer>Pictures drawn by WOOF's renderer. Open a picture in a new tab for full size.</footer>
<script>
const DATA = {data};
const g = document.getElementById('g');
for (const p of DATA) {{
  const card = document.createElement('section'); card.className = 'card';
  const h = document.createElement('h2'); h.textContent = p.title;
  const s = document.createElement('span'); s.textContent = p.domain; h.appendChild(s);
  const a = document.createElement('a'); a.target = '_blank';
  const img = document.createElement('img'); img.loading = 'lazy'; img.alt = p.title; a.appendChild(img);
  const ctl = document.createElement('div'); ctl.className = 'ctl';
  const r = document.createElement('input'); r.type = 'range'; r.min = 0; r.max = p.frames.length - 1; r.value = p.frames.length - 1;
  const o = document.createElement('output');
  const show = () => {{ const f = p.frames[r.value]; img.src = f.src; a.href = f.src; o.textContent = f.label; }};
  r.addEventListener('input', show); show();
  ctl.append(r, o); card.append(h, a, ctl); g.appendChild(card);
}}
</script>
</body></html>
"""


def _title(product: str) -> str:
    return product.replace("_", " ").replace("mb ", "hPa ").strip().capitalize()


def write_gallery(run_dir, record: dict, images: list, summary: dict) -> Path:
    run_dir = Path(run_dir)
    groups: dict = {}
    for im in images:
        key = (im["product"], im["domain"])
        groups.setdefault(key, []).append(im)
    data = []
    for (product, domain), frames in sorted(groups.items()):
        frames.sort(key=lambda f: f["lead_h"])
        data.append({"title": _title(product), "domain": domain,
                     "frames": [{"src": Path(f["media"]).relative_to(run_dir).as_posix(),
                                 "label": "%s  (%s)" % (f["valid"].replace("T", " "), f["frame"])}
                                for f in frames]})
    meta_items = [f"state: {record.get('state', '?')}"]
    doms = summary.get("domains") or []
    if doms:
        d = doms[0]
        dx = d.get("dx_km")
        meta_items.append(f"grid {d.get('nx')} x {d.get('ny')} x {d.get('nz')}" + (f" at {float(dx):g} km" if dx else ""))
    if summary.get("source"):
        meta_items.append(f"start data: {summary.get('source')} {summary.get('cycle') or ''}")
    if summary.get("physics_profile"):
        meta_items.append(f"physics: {summary.get('physics_profile')}")
    if record.get("label"):
        meta_items.append(f"label: {record.get('label')}")
    meta = "".join(f"<span>{html.escape(str(m))}</span>" for m in meta_items)
    page = _PAGE.format(run_id=html.escape(str(record.get("run_id", ""))), meta=meta,
                        data=json.dumps(data).replace("</", "<\\/"))
    out = run_dir / "gallery.html"
    out.write_text(page, encoding="utf-8")
    return out
