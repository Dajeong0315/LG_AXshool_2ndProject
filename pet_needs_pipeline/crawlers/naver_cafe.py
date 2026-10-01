"""네이버 카페 크롤러 (검색 API로 글 목록 → 로그인된 Selenium 브라우저로 본문·작성일).

[이용약관·준수 주의]
- 로그인은 사용자가 띄워진 브라우저에서 직접 한다. 아이디·비밀번호·쿠키를 코드나 파일에 저장하지 않는다.
- 회원공개(멤버 전용) 글은 해당 카페 매니저에게 교육 프로젝트 목적·비식별화·비공개를 알리고
  동의를 받은 카페만 config의 cafe_ids에 넣는다. 동의 없는 카페는 수집하지 않는다.
- robots.txt가 막은 경로는 기본적으로 요청하지 않는다 (sites.naver_cafe.respect_robots).
- 작성자 닉네임·댓글 작성자는 수집하지 않는다. 요청 간 1~3초 무작위 대기.
"""
import re

from crawlers.base import BaseCrawler
from crawlers.naver_search import naver_api_pages
from crawlers.utils import strip_html

LOCKED_HINTS = ("멤버에게만 공개", "카페 멤버만", "가입 후 이용", "등급이 되면", "권한이 없습니다")


class NaverCafeCrawler(BaseCrawler):
    name = "naver_cafe"
    required_env = ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.driver = None

    def _driver(self):
        if self.driver is None:
            from selenium import webdriver
            opts = webdriver.ChromeOptions()
            opts.add_argument("--lang=ko-KR")
            self.driver = webdriver.Chrome(options=opts)     # Selenium 4.10+가 드라이버를 자동으로 받는다
            self.driver.get("https://nid.naver.com/nidlogin.login")
            input("\n[네이버 카페] 열린 브라우저에서 직접 로그인한 뒤, 여기서 Enter를 누르세요... ")
        return self.driver

    def _read(self, url: str) -> tuple[str, str, str]:
        from selenium.webdriver.common.by import By
        d = self._driver()
        self.polite.sleep()
        d.get(url)
        try:
            d.switch_to.frame("cafe_main")                   # PC 카페는 본문이 iframe 안에 있다
        except Exception:
            pass
        page = d.find_element(By.TAG_NAME, "body").text
        body = ""
        for sel in ("div.se-main-container", "div.ContentRenderer", "div.article_viewer", "#tbody"):
            els = d.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                body = els[0].text.strip()
                break
        date = ""
        for sel in ("span.date", "div.article_info span.date", "td.date"):
            els = d.find_elements(By.CSS_SELECTOR, sel)
            if els and els[0].text.strip():
                date = els[0].text.strip()
                break
        if not date:
            m = re.search(r"20\d{2}\.\d{1,2}\.\d{1,2}\.?\s*\d{1,2}:\d{2}", page)
            date = m.group(0) if m else ""
        d.switch_to.default_content()
        return body, date, page

    def candidates(self, keyword):
        cafe_ids = [c.lower() for c in self.site_cfg.get("cafe_ids", [])]
        respect = self.site_cfg.get("respect_robots", True)
        for it in naver_api_pages(self, "cafearticle", self.query(keyword)):
            url = it.get("link", "")
            if cafe_ids and not any(c in (it.get("cafeurl", "") + url).lower() for c in cafe_ids):
                self.reject["other_cafe"] += 1
                continue
            if self.early_reject(url):
                continue
            if not self.polite.allowed(url, respect):
                self.reject["robots_body"] += 1
                continue
            try:
                body, date, page = self._read(url)
            except Exception as e:                           # 글 1건 실패 → 기록 후 다음 글
                self.fails.add(self.name, keyword, url, e)
                continue
            if not body or any(h in page for h in LOCKED_HINTS):
                self.reject["locked_or_empty"] += 1
                continue
            yield {"url": url, "title": strip_html(it.get("title")), "body": body, "date": date}

    def close(self):
        if self.driver:
            self.driver.quit()
