#!/usr/bin/env python3
"""
이지스퍼블리싱 구매평 모니터 → Discord 알림봇

사용법:
  python review_bot.py            # 전체 실행
  python review_bot.py --init     # 첫 실행: 현재 리뷰를 기준점으로 저장 (알림 없음)
  python review_bot.py --test     # Discord 웹훅 연결 테스트
  python review_bot.py --days 365 # 최근 N일 이내 출판 도서만 확인 (기본 730일)

config.json에 "discord_webhook" 필드를 추가하세요:
  "discord_webhook": "https://discord.com/api/webhooks/..."
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import json
import re
import time
import warnings
from datetime import datetime, date, timezone, timedelta

KST = timezone(timedelta(hours=9))
from pathlib import Path

import requests

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
ISBNS_FILE = BASE_DIR / "isbns.txt"
REVIEW_STATE_FILE = BASE_DIR / "review_state.json"
CACHE_FILE = BASE_DIR / "isbn_cache.json"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HTML_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}
JSON_HEADERS = {**HTML_HEADERS, "Accept": "application/json, text/plain, */*"}
AJAX_HEADERS = {**HTML_HEADERS, "Accept": "*/*", "X-Requested-With": "XMLHttpRequest"}

STORE_NAMES = {"aladin": "알라딘", "yes24": "예스24", "kyobo": "교보문고"}
STORE_COLORS = {"aladin": 0xEF4128, "yes24": 0xFF6B00, "kyobo": 0x009261}
STORE_EMOJI = {"aladin": "🔴", "yes24": "🟠", "kyobo": "🟢"}


# ─── 설정 / 상태 ─────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)

def load_isbns(max_days: int = 730) -> list:
    """(isbn, pub_date) 리스트 반환. max_days 이내 출판 도서만."""
    cutoff = date.today().toordinal() - max_days
    result = []
    for line in ISBNS_FILE.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        isbn = parts[0].strip()
        if len(isbn) != 13 or not isbn.isdigit():
            continue
        pub_str = parts[1].strip() if len(parts) > 1 else "2099-01-01"
        try:
            d = date.fromisoformat(pub_str)
            if d.toordinal() >= cutoff:
                result.append((isbn, pub_str))
        except ValueError:
            result.append((isbn, "unknown"))
    return result

def load_state() -> dict:
    if REVIEW_STATE_FILE.exists():
        with open(REVIEW_STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(REVIEW_STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

def text_hash(reviewer: str, text: str) -> str:
    key = f"{reviewer}:{text[:80]}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:14]

def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html_module.unescape(text)
    text = re.sub(r"회색\s*영역을\s*클릭하면\s*내용을\s*확인할\s*수\s*있습니다\.", "", text)
    text = re.sub(r"이\s*글에는\s*스포일러가\s*포함되어\s*있습니다\.\s*보시겠습니까\?", "", text)
    return re.sub(r"\s+", " ", text).strip()

def normalize_date(raw: str) -> str:
    """'2026-06-05', '2026.06.05', '2026/6/5' 등을 YYYY-MM-DD로. 실패 시 빈 문자열."""
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", raw or "")
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


# ─── 알라딘 ──────────────────────────────────────────────────────────
def get_aladin_reviews(isbn: str, cache: dict) -> list:
    title_key = f"_aladin_title_{isbn}"
    item_key = f"_aladin_item_{isbn}"

    try:
        # 상품 페이지 → ItemId + 제목
        if not cache.get(item_key) or not cache.get(title_key):
            r = requests.get(
                f"https://www.aladin.co.kr/shop/wproduct.aspx?ISBN={isbn}",
                headers=HTML_HEADERS, timeout=15,
            )
            if r.status_code != 200:
                return []
            # ItemId: URL 또는 페이지 내 모든 매치 중 0이 아닌 첫번째
            all_ids = re.findall(r'ItemId=(\d+)', r.url + r.text)
            item_id = next((x for x in all_ids if x != "0"), None)
            if item_id:
                cache[item_key] = item_id
            if not cache.get(title_key):
                tm = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', r.text)
                cache[title_key] = tm.group(1).strip() if tm else isbn

        item_id = cache.get(item_key)
        title = cache.get(title_key, isbn)
        if not item_id:
            return []

        referer = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
        results = []

        # 구매평 (CommentReview)
        r1 = requests.get(
            f"https://www.aladin.co.kr/ucl/shop/product/ajax/GetCommunityListAjax.aspx"
            f"?ProductItemId={item_id}&itemId={item_id}&pageCount=10"
            f"&communitytype=CommentReview&nemoType=-1&page=1"
            f"&startNumber=1&endNumber=10&sort=2&IsOrderer=2&BranchType=1"
            f"&IsAjax=true&pageType=0",
            headers={**AJAX_HEADERS, "Referer": referer}, timeout=15,
        )
        if r1.status_code == 200 and "없습니다" not in r1.text:
            results.extend(_parse_aladin_comment_reviews(r1.text, title, item_id))

        # 일반 리뷰 (MyReview)
        r2 = requests.get(
            f"https://www.aladin.co.kr/ucl/shop/product/ajax/GetCommunityListAjax.aspx"
            f"?ProductItemId={item_id}&itemId={item_id}&pageCount=10"
            f"&communitytype=MyReview&nemoType=-1&page=1"
            f"&startNumber=1&endNumber=10&sort=2&IsOrderer=2&BranchType=1"
            f"&IsAjax=true&pageType=0",
            headers={**AJAX_HEADERS, "Referer": referer}, timeout=15,
        )
        if r2.status_code == 200 and "없습니다" not in r2.text:
            results.extend(_parse_aladin_my_reviews(r2.text, title, item_id))

        return results

    except Exception as e:
        print(f"    [알라딘 오류] {isbn}: {e}")
        return []


def _parse_aladin_comment_reviews(html: str, title: str, item_id: str) -> list:
    reviews = []
    # hundred_list 블록 단위로 리뷰 분리
    blocks = re.split(r'<div class="hundred_list">', html)

    for block in blocks[1:]:
        # 리뷰 ID (commentReviewPaper{N})
        id_m = re.search(r'commentReviewPaper(\d+)', block)
        rv_id = id_m.group(1) if id_m else None
        if not rv_id:
            continue

        # 별점: star_on 이미지 개수
        stars = len(re.findall(r"icon_star_on\.png", block))

        # 리뷰어명: Ere_sub_gray8 클래스 링크 텍스트 (blog.aladin.co.kr)
        reviewer_m = re.search(r'href="https://blog\.aladin\.co\.kr/\d+[^"]*"[^>]*>([^<]+)</a>', block)
        reviewer = reviewer_m.group(1).strip() if reviewer_m else "익명"

        # 작성일: Ere_sub_gray8 스팬 중 날짜 형식
        date_m = re.search(r'<span[^>]*Ere_sub_gray8[^>]*>\s*(\d{4}[-./]\d{1,2}[-./]\d{1,2})\s*</span>', block)
        posted = normalize_date(date_m.group(1)) if date_m else ""

        # 리뷰 텍스트: <span id="spnPaper{id}"> 안에 있음
        text_m = re.search(rf'id="spnPaper{rv_id}"[^>]*>(.*?)</span>', block, re.DOTALL)
        text = clean_text(text_m.group(1)) if text_m else ""

        if text and len(text) > 5:
            reviews.append({
                "id": rv_id,
                "reviewer": reviewer,
                "rating": str(stars),
                "date": posted,
                "text": text,
                "title": title,
                "link": f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}#coReview",
            })

    return reviews


def _parse_aladin_my_reviews(html: str, title: str, item_id: str) -> list:
    """알라딘 일반 리뷰(MyReview) 파싱"""
    reviews = []
    blocks = re.split(r'<div class="hundred_list">', html)

    for block in blocks[1:]:
        # 리뷰 ID (fn_toggle_mypaper에서)
        id_m = re.search(r"fn_toggle_mypaper\((\d+)", block)
        rv_id = id_m.group(1) if id_m else None
        if not rv_id:
            continue

        stars = len(re.findall(r"icon_star_on\.png", block))

        # 리뷰 제목 (Ere_str)
        title_m = re.search(r'class="Ere_str">([^<]+)', block)
        rv_title = title_m.group(1).strip() if title_m else ""

        # 리뷰어명
        reviewer_m = re.search(r'blog\.aladin\.co\.kr/(\d+)/', block)
        reviewer = reviewer_m.group(1) if reviewer_m else "익명"

        # 작성일 (Ere_sub_gray8 스팬)
        date_m = re.search(r'Ere_sub_gray8[^>]*>\s*(\d{4}[-./]\d{1,2}[-./]\d{1,2})', block)
        posted = normalize_date(date_m.group(1)) if date_m else ""

        # 본문 (paperShort_{id})
        text_m = re.search(rf'id="paperShort_{rv_id}"[^>]*>(.*?)</div>', block, re.DOTALL)
        text = clean_text(text_m.group(1)) if text_m else rv_title

        if text and len(text) > 5:
            reviews.append({
                "id": f"M{rv_id}",  # CommentReview ID와 구분
                "reviewer": reviewer,
                "rating": str(stars),
                "date": posted,
                "text": f"[일반 리뷰] {rv_title}\n{text}" if rv_title and rv_title not in text else text,
                "title": title,
                "link": f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}#myReview",
            })

    return reviews


# ─── 예스24 ──────────────────────────────────────────────────────────
_yes24_session = requests.Session()
_yes24_session.headers.update(HTML_HEADERS)

def yes24_batch_find_ids(isbns: list, cache: dict):
    """Playwright로 Yes24 홈 세션을 맺은 뒤 ISBN 목록의 product ID를 일괄 캐싱"""
    missing = [isbn for isbn in isbns if not cache.get(f"_yes24_pid_{isbn}")]
    if not missing:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=_UA,
                locale="ko-KR",
                timezone_id="Asia/Seoul",
            )
            page = ctx.new_page()
            page.goto("https://www.yes24.com/", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=8000)

            for isbn in missing:
                try:
                    page.goto(
                        f"https://www.yes24.com/Product/Search?query={isbn}&domain=BOOK",
                        timeout=15000,
                    )
                    page.wait_for_load_state("networkidle", timeout=8000)
                    html = page.content()
                    # onclick에 ISBN이 포함된 패턴에서 product ID 추출
                    # 예: setGoodsClickExtraCodeHub('029','9791163037873','163301895','0',this)
                    m = re.search(
                        rf"setGoodsClickExtraCodeHub\('[^']*',\s*'{re.escape(isbn)}',\s*'(\d+)'",
                        html,
                    )
                    if not m:
                        # 이미지 URL 패턴: image.yes24.com/goods/{id}/L
                        # 이미지 alt에 제목이 있는 경우 근처에서 ID 추출
                        m = re.search(
                            r"image\.yes24\.com/goods/(\d+)/[LMS]",
                            html[max(0, html.find(isbn)-500):html.find(isbn)+500] if isbn in html else "",
                        )
                    if m:
                        pid = m.group(1)
                        # 제목은 알라딘 캐시 우선, 없으면 검색결과에서 추출
                        title = (
                            cache.get(f"_aladin_title_{isbn}")
                            or cache.get(f"_title_{isbn}")
                            or isbn
                        )
                        cache[f"_yes24_pid_{isbn}"] = pid
                        cache[f"_yes24_title_{isbn}"] = title
                        print(f"    [예스24] {isbn} → {pid} ({title[:30]})")
                except Exception:
                    pass
                time.sleep(0.5)
            browser.close()
    except Exception as e:
        print(f"    [예스24 Playwright 오류] {e}")


def _yes24_product_id(isbn: str, cache: dict) -> tuple:
    """(product_id, title) 반환"""
    id_key = f"_yes24_pid_{isbn}"
    title_key = f"_yes24_title_{isbn}"
    return cache.get(id_key), cache.get(title_key, isbn)


def get_yes24_reviews(isbn: str, cache: dict) -> list:
    pid, title = _yes24_product_id(isbn, cache)
    if not pid:
        return []
    try:
        url = (
            f"https://www.yes24.com/Product/communityModules/GoodsReviewList/{pid}"
            f"?goodsSetYn=N&Sort=1&PageNumber=1&Type=ALL"
        )
        r = _yes24_session.get(url, headers={
            "Accept": "text/html,*/*",
            "Referer": f"https://www.yes24.com/Product/Goods/{pid}",
        }, timeout=15)
        if r.status_code != 200:
            return []
        return _parse_yes24_reviews(r.text, title, pid)
    except Exception as e:
        print(f"    [예스24 오류] {isbn}: {e}")
        return []


def _parse_yes24_reviews(html: str, title: str, pid: str) -> list:
    reviews = []
    blocks = re.split(r'<div class="reviewInfoGrp', html)

    for block in blocks[1:]:
        # 리뷰 ID (OpenReviewReport 에서)
        id_m = re.search(r"OpenReviewReport\((\d+)\)", block)
        rv_id = id_m.group(1) if id_m else None
        if not rv_id:
            continue

        # 별점 (10점 기준)
        rating_m = re.search(r"total_rating_(\d+)", block)
        rating = rating_m.group(1) if rating_m else ""

        # 리뷰어
        reviewer_m = re.search(
            r'class="lnk_id">([^<]+)</a>', block
        )
        reviewer = reviewer_m.group(1).strip() if reviewer_m else "익명"

        # 날짜
        date_m = re.search(r'txt_date">([^<]+)</em>', block)
        date_str = date_m.group(1).strip() if date_m else ""

        # 리뷰 텍스트 (review_cont div)
        text_m = re.search(r'class="review_cont"[^>]*>(.*?)</div>', block, re.DOTALL)
        if not text_m:
            text_m = re.search(r'reviewInfoBot[^>]*>.*?<div[^>]*>(.*?)</div>', block, re.DOTALL)
        text = clean_text(text_m.group(1)) if text_m else ""

        if text and len(text) > 5:
            reviews.append({
                "id": rv_id,
                "reviewer": reviewer,
                "rating": f"{int(rating)//2}점/5점" if rating else "",
                "date": normalize_date(date_str),
                "text": text,
                "title": title,
                "link": f"https://www.yes24.com/Product/Goods/{pid}#review",
            })

    return reviews


# ─── 교보문고 ─────────────────────────────────────────────────────────
def _kyobo_product_id(isbn: str, cache: dict) -> str | None:
    if cache.get(isbn):
        return cache[isbn]
    try:
        r = requests.get(
            f"https://search.kyobobook.co.kr/search?keyword={isbn}&gbCode=TOT&target=total",
            headers={**HTML_HEADERS, "Referer": "https://www.kyobobook.co.kr/"},
            timeout=15,
        )
        m = re.search(rf'data-pid="(S\d+)"[^>]*data-bid="{isbn}"', r.text)
        if not m:
            m = re.search(rf'data-bid="{isbn}"[^>]*data-pid="(S\d+)"', r.text)
        if m:
            cache[isbn] = m.group(1)
            return cache[isbn]
    except Exception:
        pass
    return None


def _kyobo_title(isbn: str, product_id: str, cache: dict) -> str:
    title_key = f"_title_{isbn}"
    if cache.get(title_key):
        return cache[title_key]
    try:
        rp = requests.get(
            f"https://product.kyobobook.co.kr/detail/{product_id}",
            headers=HTML_HEADERS, timeout=15,
        )
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', rp.text)
        if m:
            title = m.group(1).strip()
            cache[title_key] = title
            return title
        return isbn  # 조회 실패 시 캐시 안 함 → 다음 실행 때 재시도
    except Exception:
        return isbn


def get_kyobo_reviews(isbn: str, cache: dict) -> tuple:
    """(product_id, reviews, title) 반환. 교보 리뷰 목록 API를 직접 호출(requests).
    리뷰는 페이지 스크롤 시 지연 로딩되지만, 페이지가 호출하는 API를 그대로 호출하면
    Playwright 없이 바로 받을 수 있다.
    별점 revwRvgr(1~4)는 10점 만점 환산(×2.5)하여 'N점/10점'으로 저장
    → 대시보드 parseRating이 5점으로 환산해 표시."""
    product_id = _kyobo_product_id(isbn, cache)
    if not product_id:
        return None, [], isbn

    title = _kyobo_title(isbn, product_id, cache)
    reviews = []

    try:
        # revwPatrCode=000: 전체 리뷰(구매평+일반), reviewSort=002: 최신순
        url = (
            "https://product.kyobobook.co.kr/api/review/list"
            "?page=1&pageLimit=50&reviewSort=002&revwPatrCode=000"
            f"&saleCmdtids={product_id}&webToonYsno=N&allYsno=N"
            f"&revwSummeryYn=Y&saleCmdtid={product_id}"
        )
        r = requests.get(
            url,
            headers={**JSON_HEADERS, "Referer": f"https://product.kyobobook.co.kr/detail/{product_id}"},
            timeout=15,
        )
        if r.status_code != 200 or not r.text:
            return product_id, [], title

        for rv in (r.json().get("data") or {}).get("reviewList") or []:
            if rv.get("dltYsno") == "Y":  # 삭제된 리뷰 제외
                continue
            rv_id = str(rv.get("revwNum", ""))
            text = clean_text(rv.get("revwCntt") or "")
            if not rv_id or not text:
                continue
            rvgr = rv.get("revwRvgr")
            rating = f"{rvgr * 2.5:g}점/10점" if rvgr else ""
            date_raw = rv.get("cretDttm") or ""
            reviews.append({
                "id": rv_id,
                "text": text,
                "rating": rating,
                "reviewer": rv.get("mmbrId") or "",
                "date": date_raw[:10] if date_raw else "",
                "title": title,
                "link": f"https://product.kyobobook.co.kr/detail/{product_id}#review",
            })
    except Exception as e:
        print(f"    [교보 오류] {isbn}: {e}")

    return product_id, reviews, title


# ─── Discord 발송 ─────────────────────────────────────────────────────
DASHBOARD_URL = "https://marketer-h.github.io/review-alarm/"
REVIEWS_LOG_FILE = BASE_DIR / "reviews_log.json"

def save_reviews_log(new_reviews: list):
    """새 리뷰를 reviews_log.json에 누적 저장 (최근 180일치 유지)
    date는 리뷰 작성일 기준. 작성일을 못 구한 경우(교보 등)만 감지일로 대체."""
    today = datetime.now(KST).strftime("%Y-%m-%d")
    log = []
    if REVIEWS_LOG_FILE.exists():
        with open(REVIEWS_LOG_FILE) as f:
            log = json.load(f)
    for rv in new_reviews:
        log.append({
            "date":     rv.get("date") or today,
            "isbn":     rv.get("isbn", ""),
            "title":    rv.get("title", ""),
            "store":    rv.get("store", ""),
            "id":       rv.get("id", ""),
            "reviewer": rv.get("reviewer", ""),
            "rating":   rv.get("rating", ""),
            "text":     rv.get("text", ""),
            "link":     rv.get("link", ""),
        })
    # 180일치만 유지
    cutoff = (datetime.now().toordinal() - 180)
    log = [r for r in log if date.fromisoformat(r["date"]).toordinal() >= cutoff]
    with open(REVIEWS_LOG_FILE, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def build_daily_embed(new_reviews: list) -> dict:
    """간단 요약 알림 embed: 총건수 + 서점별 + 책별 건수 + 대응 필요만. 리뷰 본문은 대시보드로."""
    from collections import Counter
    today = datetime.now(KST).strftime("%Y-%m-%d")
    total = len(new_reviews)
    flagged = [rv for rv in new_reviews if needs_response(rv)]

    sc = Counter(rv.get("store", "") for rv in new_reviews)
    store_line = " · ".join(
        f"{STORE_EMOJI.get(s, '▪️')} {STORE_NAMES.get(s, s)} {c}" for s, c in sc.most_common()
    )

    by_book = Counter((rv.get("title") or "").split("|")[0].strip() for rv in new_reviews)
    parts = [store_line, ""]
    parts += [f"• {b[:30]} — {c}" for b, c in by_book.most_common(10)]
    if len(by_book) > 10:
        parts.append(f"• 외 {len(by_book) - 10}권")

    if flagged:
        parts += ["", "🚨 **대응 필요**"]
        for rv in flagged[:5]:
            b = (rv.get("title") or "").split("|")[0].strip()
            n = rating_to_5(rv.get("rating"))
            star = f"★{n:g} · " if n is not None else ""
            parts.append(f"• 《{b[:24]}》 {star}{STORE_NAMES.get(rv.get('store', ''), '')}")
        if len(flagged) > 5:
            parts.append(f"…외 {len(flagged) - 5}건")

    parts += ["", "👉 자세한 내용은 대시보드에서 확인하세요"]

    title_line = f"📬 오늘 새 리뷰 {total}개"
    if flagged:
        title_line += f"  ·  🚨 대응 필요 {len(flagged)}건"

    return {
        "title": title_line,
        "description": "\n".join(parts),
        "color": 0x3B82F6,
        "fields": [{"name": "전체 리뷰 보기", "value": f"[대시보드 열기]({DASHBOARD_URL})", "inline": False}],
        "footer": {"text": f"이지스퍼블리싱 서점 리뷰 봇 · {today}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def send_discord(webhook_url: str, new_reviews: list):
    """간단 요약 알림 발송. webhook_url은 쉼표 구분 다중 URL 허용."""
    if not webhook_url:
        print("[Discord] config.json에 discord_webhook URL이 없습니다.")
        return
    if not new_reviews:
        return

    embed = build_daily_embed(new_reviews)
    total_count = len(new_reviews)
    for url in (u.strip() for u in webhook_url.split(",") if u.strip()):
        try:
            r = requests.post(url, json={"embeds": [embed]}, timeout=10)
            if r.status_code not in (200, 204):
                print(f"[Discord] 발송 실패: HTTP {r.status_code} ({url[:60]}…)")
            else:
                print(f"[Discord] 발송 완료 ({total_count}개) → {url[:60]}…")
        except Exception as e:
            print(f"[Discord] 오류: {e}")


def send_discord_no_reviews(webhook_url: str):
    """새 리뷰가 없는 날 보내는 짧은 하트비트 메시지. 봇이 살아있음을 알려준다."""
    if not webhook_url:
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    embed = {
        "title": "✅ 오늘 새 리뷰 없음",
        "description": "오늘은 새로 올라온 서점 리뷰가 없습니다.\n봇은 정상 작동 중입니다.",
        "color": 0x94A3B8,
        "fields": [{"name": "전체 리뷰 보기", "value": f"[대시보드 열기]({DASHBOARD_URL})", "inline": False}],
        "footer": {"text": f"이지스퍼블리싱 서점 리뷰 봇 · {today}"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    urls = [u.strip() for u in webhook_url.split(",") if u.strip()]
    for url in urls:
        try:
            r = requests.post(url, json={"embeds": [embed]}, timeout=10)
            if r.status_code not in (200, 204):
                print(f"[Discord] 발송 실패: HTTP {r.status_code} ({url[:60]}…)")
            else:
                print(f"[Discord] '리뷰 없음' 발송 완료 → {url[:60]}…")
        except Exception as e:
            print(f"[Discord] 오류: {e}")


# ─── 주간 요약 (금요일 발송) ─────────────────────────────────────────
def _summary_clean(text: str) -> str:
    """주간 요약·주목 후기용 간단 정리: [~리뷰] 태그·협찬 고지 문장 제거."""
    t = re.sub(r"\[[^\]]*리뷰\]", " ", text or "")
    bad = re.compile(r"(제공\s*받|지원\s*받|협찬|무상|서평\s*이벤트|체험단|증정|소정의|원고료)")
    t = " ".join(s for s in re.split(r"(?<=[.!?。])", t) if not bad.search(s))
    return re.sub(r"\s+", " ", t).strip()


def build_weekly_summary_embed():
    """reviews_log.json에서 최근 7일치를 집계해 주간 요약 embed 생성. 데이터 없으면 None."""
    if not REVIEWS_LOG_FILE.exists():
        return None
    with open(REVIEWS_LOG_FILE) as f:
        log = json.load(f)

    from collections import Counter
    today = datetime.now(KST).date()
    start = today - timedelta(days=6)
    wk = []
    for r in log:
        try:
            d = date.fromisoformat(r.get("date", ""))
        except ValueError:
            continue
        if start <= d <= today:
            wk.append(r)
    if not wk:
        return None

    sc = Counter(r.get("store", "") for r in wk)
    ratings = [x for x in (rating_to_5(r.get("rating")) for r in wk) if x is not None]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    books = Counter((r.get("title") or "").split("|")[0].strip() for r in wk)
    low = [r for r in wk if (rating_to_5(r.get("rating")) or 5) <= 2]

    lines = [f"새 리뷰 **{len(wk)}건**" + (f" · 평균 ⭐**{avg}**" if avg is not None else "")]
    store_line = " · ".join(
        f"{STORE_EMOJI.get(s, '▪️')} {STORE_NAMES.get(s, s)} {c}" for s, c in sc.most_common()
    )
    if store_line:
        lines.append(f"└ {store_line}")

    if low:
        lines.append(f"\n🚨 **대응 필요(★2 이하) {len(low)}건**")
        for r in low[:5]:
            t = (r.get("title") or "").split("|")[0].strip()
            snip = _summary_clean(r.get("text", ""))[:50]
            lines.append(f"• 《{t[:24]}》 ★{rating_to_5(r.get('rating'))} — {snip}…")
        if len(low) > 5:
            lines.append(f"…외 {len(low) - 5}건")
    else:
        lines.append("\n🚨 대응 필요(★2 이하): **0건** 👍")

    top = books.most_common(3)
    if top:
        lines.append("\n📚 **리뷰 많은 책 TOP**")
        medal = ["①", "②", "③"]
        for i, (b, c) in enumerate(top):
            lines.append(f"{medal[i]} {b[:28]} — {c}건")

    # 주목 후기: 이번 주 리뷰가 많은 책의 '엄선 마케팅 문구' 우선, 없으면 원문에서 추출
    highlight = None
    mq_file = BASE_DIR / "marketing_quotes.json"
    if mq_file.exists():
        try:
            with open(mq_file) as f:
                mq = json.load(f)
            for b, _ in books.most_common():
                cand = next((q for q in mq if q.get("book") == b), None)
                if cand:
                    highlight = f"\"{cand.get('quote', '')}\" — 《{b[:24]}》 ⭐{cand.get('rating', 5)}"
                    break
        except Exception:
            pass
    if not highlight:
        for r in sorted(
            (r for r in wk if (rating_to_5(r.get("rating")) or 0) >= 4.5),
            key=lambda r: len(_summary_clean(r.get("text", ""))), reverse=True,
        ):
            q = _summary_clean(r.get("text", ""))
            if len(q) >= 30:
                if len(q) > 90:
                    q = q[:90].rsplit(" ", 1)[0] + "…"
                t = (r.get("title") or "").split("|")[0].strip()
                highlight = f"\"{q}\" — 《{t[:24]}》 ⭐{rating_to_5(r.get('rating'))}"
                break
    if highlight:
        lines.append(f"\n✨ **주목 후기**\n{highlight}")

    return {
        "title": f"📊 주간 리뷰 요약 · {start.month}/{start.day}~{today.month}/{today.day}",
        "description": "\n".join(lines),
        "color": 0x6366F1,
        "fields": [{"name": "전체 보기", "value": f"[대시보드 열기]({DASHBOARD_URL})", "inline": False}],
        "footer": {"text": "이지스퍼블리싱 서점 리뷰 봇 · 주간 요약"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


def send_weekly_summary(webhook_url: str):
    """주간 요약을 (기존 일일 알림과 같은) 웹훅으로 발송. 금요일에만 호출된다."""
    if not webhook_url:
        return
    embed = build_weekly_summary_embed()
    if not embed:
        print("[주간 요약] 최근 7일 데이터가 없어 생략")
        return
    for url in (u.strip() for u in webhook_url.split(",") if u.strip()):
        try:
            r = requests.post(url, json={"embeds": [embed]}, timeout=10)
            if r.status_code not in (200, 204):
                print(f"[주간 요약] 발송 실패: HTTP {r.status_code} ({url[:60]}…)")
            else:
                print(f"[주간 요약] 발송 완료 → {url[:60]}…")
        except Exception as e:
            print(f"[주간 요약] 오류: {e}")


# ─── 제목/최신성 헬퍼 ────────────────────────────────────────────────
# 알림에 포함할 리뷰의 최대 나이(일). 기준점 누락 등으로 과거 리뷰가
# 뒤늦게 '새 리뷰'로 잡혀 알림이 가는 것을 방지한다.
ALERT_MAX_AGE_DAYS = 30

# 대응(응대)이 필요한 리뷰를 가려내는 문제 키워드
ALERT_KEYWORDS = (
    "환불", "반품", "파본", "파손", "훼손", "오타", "오류", "에러",
    "안 돼", "안돼", "안 됨", "안됨", "작동 안", "실행 안", "동작 안",
    "실망", "최악", "비추", "누락", "불량", "엉터리", "쓰레기",
)

def rating_to_5(raw):
    """별점 문자열을 5점 만점으로 환산. 실패 시 None. (대시보드 parseRating과 동일 규칙)"""
    if not raw:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*점?\s*(?:/\s*(\d+(?:\.\d+)?)\s*점?)?", str(raw))
    if not m:
        return None
    n = float(m.group(1))
    mx = float(m.group(2)) if m.group(2) else (10.0 if n > 5 else 5.0)
    if not mx or n > mx:
        return None
    return round(n / mx * 5, 1)

def needs_response(rv: dict) -> bool:
    """대응이 필요한 리뷰: 5점 환산 ★2 이하 또는 본문에 문제 키워드 포함."""
    n = rating_to_5(rv.get("rating"))
    if n is not None and n <= 2:
        return True
    return any(k in (rv.get("text") or "") for k in ALERT_KEYWORDS)

def best_title(isbn: str, cache: dict) -> str:
    """서점별로 제목이 다르게(또는 ISBN으로) 잡히는 문제 방지: 캐시에서
    가장 신뢰도 높은 제목 하나를 골라 모든 서점 리뷰에 통일 적용."""
    for key in (f"_aladin_title_{isbn}", f"_title_{isbn}", f"_yes24_title_{isbn}"):
        t = cache.get(key)
        if t and not str(t).strip().isdigit():
            return t
    return isbn

def _is_recent(date_str: str) -> bool:
    """리뷰 작성일이 ALERT_MAX_AGE_DAYS 이내인지. 날짜 미상은 포함."""
    if not date_str:
        return True
    try:
        return date.fromisoformat(date_str).toordinal() >= date.today().toordinal() - ALERT_MAX_AGE_DAYS
    except ValueError:
        return True


# ─── 메인 ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="이지스퍼블리싱 구매평 Discord 알림봇")
    parser.add_argument("--init", action="store_true", help="첫 실행: 현재 리뷰 기준점 저장 (알림 없음)")
    parser.add_argument("--test", action="store_true", help="Discord 웹훅 연결 테스트")
    parser.add_argument("--days", type=int, default=730, help="최근 N일 이내 출판 도서만 (기본 730)")
    parser.add_argument("--no-discord", action="store_true", help="수집은 하되 Discord 알림은 보내지 않음 (테스트용)")
    args = parser.parse_args()

    config = load_config()
    webhook_url = config.get("discord_webhook", "")
    stores = config.get("stores", ["aladin", "yes24", "kyobo"])

    if args.test:
        print("[테스트] Discord 웹훅 연결 확인 중...")
        if not webhook_url:
            print("  오류: config.json에 discord_webhook가 없습니다.")
            return
        r = requests.post(
            webhook_url,
            json={"content": "✅ 이지스퍼블리싱 구매평 알림봇 연결 테스트 성공!"},
            timeout=10,
        )
        print(f"  응답: HTTP {r.status_code}")
        return

    isbn_list = load_isbns(max_days=args.days)
    state = load_state()
    cache = load_cache()

    mode = "[초기화]" if args.init else "[모니터링]"
    print(f"{mode} {datetime.now():%Y-%m-%d %H:%M} — ISBN {len(isbn_list)}개, 서점 {stores}\n")

    # 예스24 product ID 일괄 조회 (미캐싱 ISBN만)
    if "yes24" in stores:
        isbns_only = [isbn for isbn, _ in isbn_list]
        missing = [i for i in isbns_only if not cache.get(f"_yes24_pid_{i}")]
        if missing:
            print(f"[예스24] product ID 조회 중 ({len(missing)}개)...")
            yes24_batch_find_ids(missing, cache)
            save_cache(cache)

    all_new: list = []

    for isbn, pub_date in isbn_list:
        if isbn not in state:
            state[isbn] = {}

        found = []

        if "aladin" in stores:
            seen = set(state[isbn].get("aladin", []))
            reviews = get_aladin_reviews(isbn, cache)
            new = [rv for rv in reviews if rv["id"] not in seen]
            if new:
                for rv in new:
                    rv["store"] = "aladin"
                    rv["isbn"] = isbn
                found.extend(new)
            all_ids = list(seen | {rv["id"] for rv in reviews})
            state[isbn]["aladin"] = all_ids[-300:]

        if "yes24" in stores:
            seen = set(state[isbn].get("yes24", []))
            reviews = get_yes24_reviews(isbn, cache)
            new = [rv for rv in reviews if rv["id"] not in seen]
            if new:
                for rv in new:
                    rv["store"] = "yes24"
                    rv["isbn"] = isbn
                found.extend(new)
            all_ids = list(seen | {rv["id"] for rv in reviews})
            state[isbn]["yes24"] = all_ids[-300:]

        if "kyobo" in stores:
            seen = set(state[isbn].get("kyobo", []))
            product_id, reviews, _ = get_kyobo_reviews(isbn, cache)
            new = [rv for rv in reviews if rv["id"] not in seen]
            if new:
                for rv in new:
                    rv["store"] = "kyobo"
                    rv["isbn"] = isbn
                found.extend(new)
            all_ids = list(seen | {rv["id"] for rv in reviews})
            state[isbn]["kyobo"] = all_ids[-300:]

        if found:
            # 서점 간 제목 통일 (그룹화 깨짐 방지)
            title = best_title(isbn, cache)
            for rv in found:
                rv["title"] = title
            print(f"  {isbn} ({title[:30]}) → 새 구매평 {len(found)}개")
            if not args.init:
                recent = [rv for rv in found if _is_recent(rv.get("date"))]
                stale = len(found) - len(recent)
                if stale:
                    print(f"    (작성일 {ALERT_MAX_AGE_DAYS}일 초과 {stale}건은 알림 제외)")
                all_new.extend(recent)

        time.sleep(0.4)

    save_state(state)
    save_cache(cache)

    print(f"\n[완료] 새 서점 리뷰 {len(all_new)}개")

    if args.init:
        print("[초기화] 기준점이 저장됐습니다. 이제 cron으로 정기 실행하세요.")
        print(f"  bash {BASE_DIR}/setup_cron.sh")
    elif all_new:
        save_reviews_log(all_new)
        if args.no_discord:
            print(f"[테스트] --no-discord: 알림 생략 (새 리뷰 {len(all_new)}개는 수집·저장됨)")
        else:
            send_discord(webhook_url, all_new)
    else:
        print("[알림 없음] 새 서점 리뷰가 없습니다.")
        if args.no_discord:
            print("[테스트] --no-discord: '리뷰 없음' 알림도 생략")
        else:
            send_discord_no_reviews(webhook_url)

    # 금요일이면 일일 알림에 이어 '주간 요약'도 발송 (KST 기준, 월=0…금=4)
    if not args.init and datetime.now(KST).weekday() == 4:
        if args.no_discord:
            print("[테스트] --no-discord: 주간 요약 생략")
        else:
            send_weekly_summary(webhook_url)


if __name__ == "__main__":
    main()
