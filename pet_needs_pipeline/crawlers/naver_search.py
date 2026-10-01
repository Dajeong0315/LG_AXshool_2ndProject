"""네이버 블로그 · 지식iN 크롤러 (네이버 검색 API + 공개 본문).

[이용약관·준수 주의]
- 검색 결과는 공식 네이버 검색 API로만 받는다. 키는 환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET.
  개발자센터 키는 naver_api.mode: legacy, 네이버 클라우드 API HUB 키는 mode: hub
- 네이버 이용약관은 사전 동의 없는 자동화 수단의 정보 수집을 제한할 수 있다 → 원문 확인 후 사용,
  수집 결과는 팀 내부 분석에만 쓰고 원문을 재배포하지 않는다.
- 본문 페이지는 robots.txt가 허용할 때만 요청한다. 막히면 API 요약문(description)만 쓴다.
- 블로거명·질문자 ID는 저장하지 않는다. 요청 간 1~3초 무작위 대기.
"""
import os
import re

from bs4 import BeautifulSoup

from crawlers.base import BaseCrawler
from crawlers.utils import strip_html


def naver_api_pages(crawler: BaseCrawler, kind: str, query: str, sort: str = "date"):
    """검색 API를 start=1부터 끝까지 넘기며 item을 하나씩 내보낸다."""
    cid, secret = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        raise RuntimeError("환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 없습니다")
    api = crawler.cfg["naver_api"]
    display, max_start = api["display"], api["max_start"]
    if api.get("mode", "legacy") == "hub":                  # 네이버 클라우드 API HUB 키
        url = f"{api['hub_base_url']}/{kind}"
        headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret}
    else:                                                   # 기존 네이버 개발자센터 키
        url = f"{api['base_url']}/{kind}.json"
        headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret}
    start = 1
    while start <= max_start:
        crawler.polite.sleep()
        r = crawler.polite.session.get(url, headers=headers,
                                       params={"query": query, "display": display, "start": start, "sort": sort},
                                       timeout=15)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return
        yield from items
        if len(items) < display:
            return
        start += display


def page_text(crawler: BaseCrawler, url: str, selectors: list[str]) -> tuple[str, str]:
    """robots.txt 허용 시 본문을 가져온다. (본문, 전체 페이지 텍스트) 반환. 실패하면 ('', '')."""
    if not crawler.polite.allowed(url):
        crawler.reject["robots_body"] += 1
        return "", ""
    soup = BeautifulSoup(crawler.polite.get(url).text, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    full = soup.get_text(" ", strip=True)
    for sel in selectors:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(" ", strip=True), full
    return "", full


class NaverBlogCrawler(BaseCrawler):
    name = "naver_blog"
    required_env = ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")

    @staticmethod
    def mobile_url(link: str) -> str:
        m = re.search(r"blog\.naver\.com/(?:PostView\.naver\?blogId=)?([\w-]+)[/&](?:logNo=)?(\d+)", link)
        return f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}" if m else link

    def candidates(self, keyword):
        for it in naver_api_pages(self, "blog", self.query(keyword)):
            url, date = it.get("link", ""), it.get("postdate")
            if self.early_reject(url):
                continue
            if self.too_old(date):          # sort=date(최신순) → 이후 결과도 전부 기간 밖
                return
            title, body, src = strip_html(it.get("title")), strip_html(it.get("description")), "snippet"
            if self.site_cfg.get("fetch_body", True) and "blog.naver.com" in url:
                try:
                    text, _ = page_text(self, self.mobile_url(url),
                                        ["div.se-main-container", "#postViewArea", "div.post_ct"])
                    if text:
                        body, src = text, "page"
                except Exception as e:
                    self.fails.add(self.name, keyword, url, f"본문 실패→요약문 사용: {e}")
            yield {"url": url, "title": title, "body": body, "date": date, "body_from": src}


class NaverKinCrawler(BaseCrawler):
    name = "naver_kin"
    required_env = ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")

    def candidates(self, keyword):
        streak, stop_at = 0, self.site_cfg.get("old_streak_stop", 30)
        for it in naver_api_pages(self, "kin", self.query(keyword)):
            url = it.get("link", "")
            if self.early_reject(url):
                continue
            if streak >= stop_at:                             # 오래된 질문만 계속 나오면 다음 키워드로
                self.reject["stop_old_streak"] += 1
                return
            title, body, src, date = strip_html(it.get("title")), strip_html(it.get("description")), "snippet", None
            try:
                text, full = page_text(self, url, ["div.questionDetail", "div.c-heading__content",
                                                   "div.se-main-container"])
                if text:
                    body, src = text, "page"
                m = re.search(r"작성일\s*(20\d{2}\.\d{1,2}\.\d{1,2})", full) or \
                    re.search(r"(20\d{2}\.\d{1,2}\.\d{1,2})\.", full)
                date = m.group(1) if m else None
            except Exception as e:
                self.fails.add(self.name, keyword, url, e)
            streak = streak + 1 if (date and self.too_old(date)) else 0
            if streak:
                continue
            yield {"url": url, "title": title, "body": body, "date": date, "body_from": src}
