"""모든 크롤러의 공통 인터페이스: collect(keyword, limit) -> DataFrame.

하위 클래스는 candidates(keyword)만 구현한다. 후보(dict)를 하나씩 내보내면
여기서 URL 중복·기간·강아지 필터를 통과한 것만 limit까지 모은다.
"""
import os
from collections import Counter

import pandas as pd

from crawlers.utils import FailureLog, Polite, parse_date, sha
from preprocess.filters import animal_check, feed_type, in_period

COLUMNS = ["source", "url", "url_hash", "title", "body", "date", "keyword", "feed_type", "rating", "body_from"]


class BaseCrawler:
    name = "base"
    required_env: tuple = ()                  # 필요한 API 키 환경변수 (없으면 사이트 전체를 건너뜀)

    def __init__(self, cfg: dict, site_cfg: dict, polite: Polite, fails: FailureLog, seen: set):
        self.cfg, self.site_cfg, self.polite, self.fails = cfg, site_cfg, polite, fails
        self.seen = seen                      # 체크포인트에 저장된 url_hash (이어하기용)
        self.reject = Counter()               # 필터별 탈락 건수
        self.exhausted = False                # 더 가져올 결과가 없으면 True
        missing = [k for k in self.required_env if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"환경변수 없음: {', '.join(missing)}")

    # 하위 클래스 구현: dict(url, title, body, date, rating?, body_from?) 를 yield
    def candidates(self, keyword: str):
        raise NotImplementedError

    def query(self, keyword: str) -> str:
        pre = self.cfg.get("query_prefix", "")
        return keyword if (not pre or "강아지" in keyword or "반려견" in keyword) else f"{pre} {keyword}"

    def early_reject(self, url: str) -> bool:
        """이미 수집한 URL이면 True (본문 요청 전에 거른다)."""
        if sha(url) in self.seen:
            self.reject["seen"] += 1
            return True
        return False

    def too_old(self, date) -> bool:
        """날짜가 수집 기간 밖이면 True."""
        d = parse_date(date)
        if d is not None and not in_period(d, self.cfg):
            self.reject["period"] += 1
            return True
        return False

    def collect(self, keyword: str, limit: int) -> pd.DataFrame:
        rows, self.exhausted = [], True
        need_dog = self.site_cfg.get("require_dog_word", True)
        try:
            for c in self.candidates(keyword):
                try:
                    h = sha(c["url"])
                    if h in self.seen:
                        self.reject["seen"] += 1
                        continue
                    d = parse_date(c.get("date"))
                    if not in_period(d, self.cfg):
                        self.reject["no_date" if d is None else "period"] += 1
                        continue
                    text = f"{c.get('title', '')} {c.get('body', '')}"
                    if animal_check(text, self.cfg, need_dog) in ("cat", "none"):
                        self.reject["not_dog"] += 1
                        continue
                    self.seen.add(h)
                    rows.append({
                        "source": self.name,
                        "url": h if self.cfg.get("anonymize_url") else c["url"],
                        "url_hash": h,
                        "title": c.get("title", ""),
                        "body": c.get("body", ""),
                        "date": d.date().isoformat(),
                        "keyword": keyword,
                        "feed_type": feed_type(text, self.cfg),
                        "rating": c.get("rating"),
                        "body_from": c.get("body_from", "page"),
                    })
                    if len(rows) >= limit:
                        self.exhausted = False
                        break
                except Exception as e:  # 게시글 1건 실패는 건너뛴다
                    self.fails.add(self.name, keyword, c.get("url", ""), e)
        except Exception as e:          # 키워드 전체 실패도 건너뛴다
            self.fails.add(self.name, keyword, "", e)
            self.exhausted = False       # 다음 실행 때 다시 시도
        return pd.DataFrame(rows, columns=COLUMNS)

    def close(self):
        pass
