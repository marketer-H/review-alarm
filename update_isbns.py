#!/usr/bin/env python3
"""이지스퍼블리싱 신간 자동 발견 → isbns.txt에 추가.

교보문고 출판사 검색을 페이징하며 이지스퍼블리싱 ISBN(prefix로 식별)을 수집하고,
isbns.txt에 아직 없는 책 중 '최근(기본 730일 이내) 출판'인 책만 출판일과 함께 추가한다.
- 봇은 730일 이내 출판 도서만 검사하므로 그보다 오래된 책은 추가하지 않는다.
- 한 번 조회한 출판일은 isbn_cache.json(_disc_pubdate_*)에 캐시해 재조회를 피한다.

사용법:
  python update_isbns.py             # 실제로 isbns.txt에 추가
  python update_isbns.py --dry-run   # 추가될 목록만 출력 (파일 변경 없음)
  python update_isbns.py --max-days 365
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
ISBNS_FILE = BASE_DIR / "isbns.txt"
CACHE_FILE = BASE_DIR / "isbn_cache.json"

# 이지스퍼블리싱 ISBN prefix (979-11-6303x 신규 / 978-89-97390 구판)
EASYS_PREFIXES = ("9791163", "9788997390")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.kyobobook.co.kr/",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "identity",
}


def load_known() -> set:
    if not ISBNS_FILE.exists():
        return set()
    known = set()
    for line in ISBNS_FILE.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 13 and parts[0].isdigit():
            known.add(parts[0])
    return known


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def crawl_kyobo_isbns(max_pages: int = 25) -> set:
    """교보 출판사 검색을 페이징하며 이지스퍼블리싱 prefix ISBN을 수집."""
    found: set = set()
    empty_streak = 0
    for page in range(1, max_pages + 1):
        url = ("https://search.kyobobook.co.kr/search?keyword=이지스퍼블리싱"
               f"&gbCode=TOT&target=total&page={page}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"  [page {page}] 요청 오류: {e}")
            break
        bids = {b for b in re.findall(r'data-bid="(97[89]\d{10})"', r.text)
                if b.startswith(EASYS_PREFIXES)}
        found |= bids
        if not bids:
            empty_streak += 1
            if empty_streak >= 2:
                break
        else:
            empty_streak = 0
        time.sleep(0.4)
    return found


def kyobo_pid(isbn: str, cache: dict) -> str | None:
    """ISBN → 교보 상품 pid(S...). isbn_cache.json의 기존 매핑 우선 사용."""
    if str(cache.get(isbn, "")).startswith("S"):
        return cache[isbn]
    try:
        sr = requests.get(
            f"https://search.kyobobook.co.kr/search?keyword={isbn}&gbCode=TOT&target=total",
            headers=HEADERS, timeout=20,
        )
        m = (re.search(rf'data-bid="{isbn}"[^>]*data-pid="(S\d+)"', sr.text)
             or re.search(rf'data-pid="(S\d+)"[^>]*data-bid="{isbn}"', sr.text))
        if m:
            cache[isbn] = m.group(1)
            return m.group(1)
    except Exception as e:
        print(f"    [pid 오류] {isbn}: {e}")
    return None


def kyobo_pubdate(isbn: str, cache: dict) -> str | None:
    """교보 상세에서 출판일(YYYY-MM-DD). 실패 시 None."""
    pid = kyobo_pid(isbn, cache)
    if not pid:
        return None
    try:
        pr = requests.get(f"https://product.kyobobook.co.kr/detail/{pid}",
                          headers=HEADERS, timeout=20)
        dm = re.search(r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', pr.text)
        if dm:
            y, mo, d = dm.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    except Exception as e:
        print(f"    [출판일 오류] {isbn}: {e}")
    return None


def main():
    parser = argparse.ArgumentParser(description="이지스퍼블리싱 신간 자동 발견")
    parser.add_argument("--dry-run", action="store_true",
                        help="추가될 목록만 출력 (파일 변경 없음)")
    parser.add_argument("--max-days", type=int, default=730,
                        help="이 일수 이내 출판된 책만 추가 (기본 730)")
    args = parser.parse_args()

    known = load_known()
    cache = load_cache()
    print(f"[발견] 기존 isbns.txt: {len(known)}개")
    print("[발견] 교보 이지스퍼블리싱 목록 크롤링...")
    found = crawl_kyobo_isbns()
    print(f"[발견] 교보 수집: {len(found)}개")

    candidates = sorted(found - known)
    if not candidates:
        print("[발견] 새 후보 없음. (모두 이미 등록됨)")
        return

    print(f"[발견] 미등록 후보 {len(candidates)}개 → 출판일 확인(캐시 활용)")
    cutoff = date.today().toordinal() - args.max_days
    to_add, too_old, no_date = [], 0, 0
    for isbn in candidates:
        pkey = f"_disc_pubdate_{isbn}"
        pub = cache.get(pkey)
        if pub is None:                       # 캐시에 없으면 조회 후 저장(성공시에만)
            pub = kyobo_pubdate(isbn, cache)
            if pub:
                cache[pkey] = pub
            time.sleep(0.3)
        if not pub:
            no_date += 1
            continue
        try:
            if date.fromisoformat(pub).toordinal() >= cutoff:
                to_add.append((isbn, pub))
            else:
                too_old += 1
        except ValueError:
            no_date += 1

    print(f"[발견] 최근({args.max_days}일 이내) 신규 {len(to_add)}개 / "
          f"오래돼 제외 {too_old}개 / 출판일미상 {no_date}개")
    for isbn, pub in sorted(to_add, key=lambda x: x[1], reverse=True):
        print(f"  + {isbn}  {pub}")

    if args.dry_run:
        print("\n[dry-run] 파일 변경 안 함.")
        return

    save_cache(cache)
    if to_add:
        with open(ISBNS_FILE, "a", encoding="utf-8") as f:
            for isbn, pub in to_add:
                f.write(f"{isbn} {pub}\n")
        print(f"\n[발견] isbns.txt에 {len(to_add)}개 추가 완료.")
    else:
        print("\n[발견] 추가할 최근 신간이 없습니다.")


if __name__ == "__main__":
    main()
