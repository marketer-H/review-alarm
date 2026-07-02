#!/usr/bin/env python3
"""marketing_quotes.json → '이주의 독자 후기' 카드뉴스(1080x1350 PNG) 생성기.

사용법:
  python generate_cards.py            # 전체 생성
  python generate_cards.py --sample   # 앞 2권만 (미리보기용)
표지는 알라딘 검색에서 자동 다운로드(캐시), 실패 시 텍스트 표지로 대체.
"""
import base64, json, pathlib, re, sys, urllib.parse
import requests
from playwright.sync_api import sync_playwright

BASE = pathlib.Path(__file__).parent
OUT = BASE / "cards"
COVER_DIR = OUT / "_covers"
OUT.mkdir(exist_ok=True); COVER_DIR.mkdir(exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
STORE = {"aladin": "알라딘", "yes24": "예스24", "kyobo": "교보문고"}

def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", s).strip()

def fetch_cover_b64(title):
    """알라딘 검색 첫 결과 표지 → base64 data URI. 실패 시 None. (책별 파일 캐시)"""
    cache = COVER_DIR / (safe_name(title) + ".jpg")
    if cache.exists():
        return "data:image/jpeg;base64," + base64.b64encode(cache.read_bytes()).decode()
    try:
        u = "https://www.aladin.co.kr/search/wsearchresult.aspx?SearchWord=" + urllib.parse.quote(title)
        r = requests.get(u, headers={"User-Agent": UA}, timeout=15)
        m = re.search(r'https://image\.aladin\.co\.kr/product/\d+/\d+/cover\w*/[^\s"\'>]+?\.(?:jpg|png)', r.text)
        if not m:
            return None
        img = requests.get(m.group(0), headers={"User-Agent": UA}, timeout=15)
        if img.status_code == 200 and len(img.content) > 2000:
            cache.write_bytes(img.content)
            return "data:image/jpeg;base64," + base64.b64encode(img.content).decode()
    except Exception as e:
        print("    [표지 실패]", title, e)
    return None

TEMPLATE = """<!doctype html><html><head><meta charset='utf-8'>
<style>
@import url('https://fonts.googleapis.com/css2?family=Gowun+Batang:wght@700&family=Noto+Sans+KR:wght@500;700&display=swap');
*{margin:0;box-sizing:border-box;}
body{width:1080px;height:1080px;background:#F4EEE3;font-family:'Noto Sans KR',sans-serif;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:58px 86px;position:relative;overflow:hidden;}
.bd{position:absolute;inset:42px;border:2px solid #E3D7C1;border-radius:26px;}
.eb{align-self:stretch;font-size:27px;font-weight:500;letter-spacing:12px;color:#B5852C;text-align:center;}
.eb .l{width:100%;height:2px;background:#D4A23A;margin:18px 0 0;}
img.cv{margin-top:44px;width:310px;height:444px;border-radius:13px;object-fit:cover;box-shadow:15px 17px 0 #E7DECB;}
.cvph{margin-top:44px;width:310px;height:444px;border-radius:13px;background:#0F2744;color:#fff;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px;font-size:34px;font-weight:700;line-height:1.3;box-shadow:15px 17px 0 #E7DECB;}
.q{font-family:'Gowun Batang',serif;font-weight:700;color:#15304E;margin-top:48px;line-height:1.62;max-width:884px;text-align:center;}
.st{color:#EBA417;font-size:38px;letter-spacing:9px;margin-top:40px;}
</style></head><body>
<div class="bd"></div>
<div class="eb">오늘의 독자 후기<div class="l"></div></div>
__COVER__
<div class="q" style="font-size:__QSIZE__px">__QUOTE__</div>
<div class="st">__STARS__</div>
</body></html>"""

def qsize(n):
    return 45 if n <= 42 else 39 if n <= 70 else 34 if n <= 100 else 30

def build_html(item, cover_b64):
    book = item["book"]
    quote = item["quote"].strip()
    n = int(round(item.get("rating", 5)))
    cover = (f"<img class='cv' src='{cover_b64}'>" if cover_b64
             else f"<div class='cvph'>{esc(book)}</div>")
    return (TEMPLATE
            .replace("__COVER__", cover)
            .replace("__QSIZE__", str(qsize(len(quote))))
            .replace("__QUOTE__", "“" + esc(quote) + "”")
            .replace("__STARS__", "★" * n))

def main():
    sample = "--sample" in sys.argv
    quotes = json.load(open(BASE / "marketing_quotes.json", encoding="utf-8"))
    # 책 등장 순서 유지
    books, seen = [], set()
    for q in quotes:
        if q["book"] not in seen:
            seen.add(q["book"]); books.append(q["book"])
    if sample:
        books = books[:2]
    targets = [q for q in quotes if q["book"] in books]

    print(f"[카드 생성] 대상 {len(targets)}장 (책 {len(books)}종){' · 샘플' if sample else ''}")
    covers = {b: fetch_cover_b64(b) for b in books}
    for b in books:
        print(f"  표지 {'OK' if covers[b] else '없음(텍스트대체)'} — {b}")

    made = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        idx = {}
        for item in targets:
            b = item["book"]; idx[b] = idx.get(b, 0) + 1
            out = OUT / f"{safe_name(b)}_{idx[b]}.png"
            pg = browser.new_page(viewport={"width": 1080, "height": 1080}, device_scale_factor=1)
            pg.set_content(build_html(item, covers[b]), wait_until="networkidle")
            pg.wait_for_timeout(700)
            pg.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": 1080, "height": 1080})
            pg.close()
            made.append(out.name)
            print(f"    저장: {out.name}")
        browser.close()
    print(f"[완료] {len(made)}장 → {OUT}")

if __name__ == "__main__":
    main()
