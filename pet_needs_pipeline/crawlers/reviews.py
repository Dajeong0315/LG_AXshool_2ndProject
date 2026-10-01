"""제품 리뷰 크롤러 2종.

1) ManualReviewCrawler — 팀원이 직접 옮겨 적은 CSV(data/manual/*.csv)를 읽는다.
   [주의] 쿠팡 robots.txt는 등록된 검색엔진 외 봇을 차단한다고 알려져 있어 자동 수집하지 않는다.
   상품당 최신 30~50건을 사람이 표본으로 옮기고, 작성자명은 적지 않는다.
   CSV 컬럼: source,url,title,body,date,rating,keyword  (keyword = config의 제품 카테고리 값)

2) ShopReviewCrawler — robots.txt가 허용하는 쇼핑몰 상품 페이지의 리뷰를 Selenium으로 읽는다.
   [주의] 각 몰의 이용약관(자동수집 금지 조항)을 먼저 확인하고, 허용될 때만 enabled: true.
   선택자는 사이트마다 달라 config에서 직접 채운다. 작성자명은 읽지 않는다.
"""
from pathlib import Path

import pandas as pd

from crawlers.base import BaseCrawler
from crawlers.utils import sha


class ManualReviewCrawler(BaseCrawler):
    name = "manual_reviews"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        files = sorted(Path(self.site_cfg.get("folder", "data/manual")).glob("*.csv"))
        frames = [pd.read_csv(f, encoding="utf-8-sig", dtype=str) for f in files]
        self.df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def candidates(self, keyword):
        if self.df.empty or "keyword" not in self.df:
            return
        for i, r in self.df[self.df["keyword"].str.strip() == keyword].iterrows():
            url = r.get("url") if isinstance(r.get("url"), str) and r.get("url") else f"manual://{keyword}/{i}"
            yield {"url": url, "title": r.get("title", "") or "", "body": r.get("body", "") or "",
                   "date": r.get("date"), "rating": pd.to_numeric(r.get("rating"), errors="coerce"),
                   "body_from": f"manual:{r.get('source', '')}"}


class ShopReviewCrawler(BaseCrawler):
    name = "shop_reviews"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.driver = None

    def _driver(self):
        if self.driver is None:
            from selenium import webdriver
            opts = webdriver.ChromeOptions()
            opts.add_argument("--headless=new")
            self.driver = webdriver.Chrome(options=opts)
        return self.driver

    def candidates(self, keyword):
        from selenium.webdriver.common.by import By
        sel = self.site_cfg.get("selectors", {})
        if not sel.get("review_item") or not sel.get("body"):
            raise RuntimeError("shop_reviews.selectors(review_item, body)를 config에 채워야 합니다")
        for p in [p for p in self.site_cfg.get("products", []) if p.get("keyword") == keyword]:
            url = p["url"]
            if not self.polite.allowed(url):
                self.reject["robots_body"] += 1
                continue
            d = self._driver()
            self.polite.sleep()
            d.get(url)
            for page in range(self.site_cfg.get("max_pages", 10)):
                items = d.find_elements(By.CSS_SELECTOR, sel["review_item"])
                for j, it in enumerate(items):
                    def txt(css):
                        els = it.find_elements(By.CSS_SELECTOR, css) if css else []
                        return els[0].text.strip() if els else ""
                    body, date = txt(sel["body"]), txt(sel.get("date"))
                    yield {"url": f"{url}#r-{sha(body + date, 12)}", "title": p.get("title", ""),  # 리뷰 순서가 바뀌어도 같은 id
                           "body": body, "date": date,
                           "rating": pd.to_numeric(txt(sel.get("rating"))[:1], errors="coerce")}
                nxt = d.find_elements(By.CSS_SELECTOR, sel["next_page"]) if sel.get("next_page") else []
                if not nxt:
                    break
                self.polite.sleep()
                nxt[0].click()

    def close(self):
        if self.driver:
            self.driver.quit()
