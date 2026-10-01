"""Print a full store's worth of fake ticket barcodes, for testing away from the store.

    pip install python-barcode
    python tools/make_test_barcodes.py            # -> test-barcodes.html

Each of the 48 boxes gets a different game that is on sale in PA right now (read
from data/state.json), so the app recognises every one. The pack numbers are made
up but start with 99, which no real PA pack does, so test data can always be told
apart from real data.

Two sheets come out, same games and packs on both:

* Count 1 — scan it as one count.
* Count 2 — the same packs further along. Scan it as a later count and the app
  should report exactly the sales listed at the end of the page.

Box 3 is left blank on both sheets on purpose, to test marking a box empty.

Codes are Code 128 carrying the dashed form printed on real tickets
(``1750-9900001-012``), which every common scan gun reads out of the box.
"""

from __future__ import annotations

import argparse
import html
import json
import pathlib
import sys
from io import BytesIO

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from lottery_tracker.packs import DEFAULT_PACK_SIZE_BY_PRICE  # noqa: E402

# A plausible 48-box store: cheaper tickets sell more, so they get more boxes.
MIX = {1.0: 5, 2.0: 6, 3.0: 3, 5.0: 11, 10.0: 10, 20.0: 5, 30.0: 6, 50.0: 2}
EMPTY_BOX = 3


def active_games(state_path: pathlib.Path) -> dict:
    games = json.loads(state_path.read_text())["games"].values()
    by_price: dict = {}
    for g in sorted(games, key=lambda g: g["game_number"], reverse=True):
        if g.get("status") == "active" and g.get("price"):
            by_price.setdefault(float(g["price"]), []).append(g)
    return by_price


def plan(by_price: dict) -> list:
    """One row per box: game, pack, and the ticket showing at each count."""
    picked = []
    for price, n in MIX.items():
        have = by_price.get(price, [])
        if len(have) < n:
            sys.exit(f"Only {len(have)} active ${price:g} games; need {n}. Adjust MIX.")
        picked += have[:n]
    assert len(picked) == 48

    rows = []
    for box, g in enumerate(picked, start=1):
        size = DEFAULT_PACK_SIZE_BY_PRICE[float(g["price"])].size
        first = max(1, size // 5 + box % 4)                 # a fifth of the way in
        sold = max(1, min(size - 1 - first, size // 6 + box % 5))
        rows.append({
            "box": box, "game": g["game_number"], "name": g["name"],
            "price": float(g["price"]), "pack": f"99{box:05d}",
            "t1": first, "t2": first + sold, "size": size,
            "empty": box == EMPTY_BOX,
        })
    return rows


def code(row: dict, ticket: int) -> str:
    return f"{row['game']}-{row['pack']}-{ticket:03d}"


def svg(text: str) -> str:
    """Code 128 as an SVG that scales without losing any bars.

    Drawn from the module pattern rather than using the library's own SVG, which
    is sized in millimetres with no viewBox: shrink it to fit a label and the
    right-hand end — stop pattern included — is cropped off, so nothing can
    read it.
    """
    from barcode import Code128
    modules = Code128(text).build()[0]           # "11010010000..." one char per module
    quiet = 10                                    # blank modules either side
    width, height = len(modules) + 2 * quiet, 50
    bars, x = [], 0
    while x < len(modules):
        if modules[x] == "1":
            run = len(modules[x:]) - len(modules[x:].lstrip("1"))
            bars.append(f'<rect x="{x + quiet}" y="0" width="{run}" height="{height}"/>')
            x += run
        else:
            x += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none" shape-rendering="crispEdges">'
            f'<rect width="{width}" height="{height}" fill="#fff"/>'
            f'<g fill="#000">{"".join(bars)}</g></svg>')


def page(rows: list) -> str:
    def sheet(n: int, key: str) -> str:
        cells = []
        for r in rows:
            head = (f"<div class='h'><b>Box {r['box']}</b>"
                    f"<span>${r['price']:g}</span></div>"
                    f"<div class='n'>#{r['game']} {html.escape(r['name'])}</div>")
            if r["empty"]:
                cells.append(f"<div class='c'>{head}<div class='blank'>EMPTY — "
                             "tap “Skip — box is empty”</div></div>")
            else:
                t = code(r, r[key])
                cells.append(f"<div class='c'>{head}<div class='bc'>{svg(t)}</div>"
                             f"<div class='t'>{t}</div></div>")
        return (f"<section><h1>Count {n}</h1><p>Scan in box order, 1 → 48.</p>"
                f"<div class='g'>{''.join(cells)}</div></section>")

    sold = [r for r in rows if not r["empty"]]
    tickets = sum(r["t2"] - r["t1"] for r in sold)
    money = sum((r["t2"] - r["t1"]) * r["price"] for r in sold)
    answers = "".join(
        f"<div class='a'><span>Box {r['box']} · #{r['game']}</span>"
        f"<span>{r['t2'] - r['t1']} × ${r['price']:g} = "
        f"<b>${(r['t2'] - r['t1']) * r['price']:,.0f}</b></span></div>"
        for r in sold)

    return f"""<!doctype html><meta charset="utf-8">
<title>Valley Lotto test barcodes</title>
<style>
  body {{ font:13px system-ui, sans-serif; margin:0; padding:16px; color:#000; background:#fff; }}
  h1 {{ margin:0 0 2px; }} p {{ margin:0 0 10px; color:#444; }}
  section {{ break-after:page; }}
  .g {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:10px; }}
  .c {{ border:1px solid #bbb; border-radius:6px; padding:8px; break-inside:avoid; }}
  .h {{ display:flex; justify-content:space-between; font-size:14px; }}
  .n {{ font-size:11px; color:#444; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .bc svg {{ width:100%; height:64px; display:block; margin-top:6px; }}
  .t {{ font:12px ui-monospace, monospace; text-align:center; letter-spacing:.5px; }}
  .blank {{ height:70px; display:flex; align-items:center; justify-content:center;
            text-align:center; color:#666; border:2px dashed #bbb; border-radius:4px; margin-top:6px; }}
  .a {{ display:flex; justify-content:space-between; gap:8px; padding:3px 0; border-top:1px solid #eee; }}
  .total {{ font-size:16px; margin:10px 0; }}
  /* Two across on paper keeps every bar about 0.37 mm wide — comfortably above
     what a retail gun needs. Three across made them too thin to rely on. */
  @media print {{ body {{ padding:0; }} .g {{ grid-template-columns:repeat(2, 1fr); }} }}
</style>
{sheet(1, "t1")}
{sheet(2, "t2")}
<section>
  <h1>What the app should say</h1>
  <p>After scanning Count 1 then Count 2 on the same day, the daily report should show:</p>
  <div class="total"><b>{tickets:,} tickets sold · ${money:,.0f}</b> across {len(sold)} boxes
    (box {EMPTY_BOX} empty)</div>
  {answers}
</section>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="test-barcodes.html")
    ap.add_argument("--state", default=str(ROOT / "data" / "state.json"))
    a = ap.parse_args()
    rows = plan(active_games(pathlib.Path(a.state)))
    pathlib.Path(a.out).write_text(page(rows))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
