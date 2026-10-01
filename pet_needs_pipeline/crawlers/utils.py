"""크롤러 공통 유틸: 대기, robots.txt, 날짜 파싱, 해시, 실패 기록."""
import csv
import hashlib
import html
import logging
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

import pandas as pd
import requests

log = logging.getLogger("pipeline")


def sha(text: str, n: int = 16) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:n]


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class Polite:
    """요청 간 무작위 대기 + robots.txt 확인 + 공용 세션."""

    def __init__(self, cfg: dict):
        self.min_s = cfg["wait"]["min_sec"]
        self.max_s = cfg["wait"]["max_sec"]
        self.respect = cfg.get("respect_robots", True)
        self.ua = cfg["user_agent"]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.ua
        self._robots = {}

    def sleep(self):
        time.sleep(random.uniform(self.min_s, self.max_s))

    def allowed(self, url: str, respect: bool | None = None) -> bool:
        if not (self.respect if respect is None else respect):
            return True
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=10)
                if r.status_code >= 400:
                    rp.parse([])            # robots.txt 없음 → 허용
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None                   # 확인 불가 → 보수적으로 차단
            self._robots[base] = rp
        rp = self._robots[base]
        return bool(rp and rp.can_fetch(self.ua, url))

    def get(self, url: str, **kw) -> requests.Response:
        self.sleep()
        r = self.session.get(url, timeout=kw.pop("timeout", 15), **kw)
        r.raise_for_status()
        return r


_DATE_PATTERNS = [
    (re.compile(r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})"), "ymd"),
    (re.compile(r"\b(20\d{2})(\d{2})(\d{2})\b"), "ymd"),
]


def parse_date(text, now: datetime | None = None):
    """'2025.10.03.', '2025-10-03T..', '20251003', '3일 전', '어제' → Timestamp. 실패 시 None."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return None
    s = str(text).strip()
    now = now or datetime.now()
    m = re.search(r"(\d+)\s*(분|시간|일|주|개월|달)\s*전", s)
    if m:
        n, u = int(m.group(1)), m.group(2)
        delta = {"분": timedelta(minutes=n), "시간": timedelta(hours=n), "일": timedelta(days=n),
                 "주": timedelta(weeks=n), "개월": timedelta(days=30 * n), "달": timedelta(days=30 * n)}[u]
        return pd.Timestamp(now - delta).normalize()
    if "어제" in s:
        return pd.Timestamp(now - timedelta(days=1)).normalize()
    if "방금" in s or "오늘" in s:
        return pd.Timestamp(now).normalize()
    for pat, _ in _DATE_PATTERNS:
        m = pat.search(s)
        if m:
            try:
                return pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


class FailureLog:
    """실패 내역을 logs/failures.csv에 한 줄씩 남긴다."""

    def __init__(self, log_dir: str):
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self.path = Path(log_dir) / "failures.csv"
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(["time", "site", "keyword", "url", "error"])

    def add(self, site, keyword, url, err):
        err = re.sub(r"((?:key|token|apikey)=)[^&\s]+", r"\1***", str(err), flags=re.I)    # 오류 메시지 속 API 키·토큰 가리기
        err = re.sub(r"apify_api_\w+", "***", err)
        with open(self.path, "a", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow([datetime.now().isoformat(timespec="seconds"), site, keyword, url, str(err)[:300]])
        log.warning("[%s] %s | %s | %s", site, keyword, url, str(err)[:200])
