"""유튜브 댓글 크롤러 (YouTube Data API v3 공식 API만 사용).

[이용약관·준수 주의]
- YouTube API 서비스 약관을 따른다. 키는 환경변수 YOUTUBE_API_KEY. 일일 쿼터(검색 1회 = 100 units) 안에서 사용.
- 채널명·댓글 작성자명·프로필은 저장하지 않는다.
"""
import os

from crawlers.base import BaseCrawler
from crawlers.utils import strip_html
from preprocess.filters import cutoff_date


class YoutubeCrawler(BaseCrawler):
    name = "youtube"
    required_env = ("YOUTUBE_API_KEY",)

    def _get(self, path, **params):
        key = os.getenv("YOUTUBE_API_KEY")
        if not key:
            raise RuntimeError("환경변수 YOUTUBE_API_KEY 가 없습니다")
        self.polite.sleep()
        r = self.polite.session.get(f"{self.cfg['youtube_api']['base_url']}/{path}",
                                    params={**params, "key": key}, timeout=15)
        r.raise_for_status()
        return r.json()

    def candidates(self, keyword):
        after = cutoff_date(self.cfg).strftime("%Y-%m-%dT00:00:00Z")
        res = self._get("search", part="snippet", q=self.query(keyword), type="video",
                        maxResults=self.site_cfg.get("videos_per_keyword", 3),
                        relevanceLanguage="ko", regionCode="KR")
        per_video = self.site_cfg.get("comments_per_video", 50)
        for v in res.get("items", []):
            vid, vtitle = v["id"]["videoId"], strip_html(v["snippet"]["title"])
            got, token = 0, None
            while got < per_video:
                try:
                    params = dict(part="snippet", videoId=vid, maxResults=100, order="time", textFormat="plainText")
                    if token:
                        params["pageToken"] = token
                    page = self._get("commentThreads", **params)
                except Exception as e:                       # 댓글 막힌 영상 등 → 다음 영상
                    self.fails.add(self.name, keyword, f"https://youtu.be/{vid}", e)
                    break
                for it in page.get("items", []):
                    s = it["snippet"]["topLevelComment"]["snippet"]
                    if s["publishedAt"] < after:             # order=time → 이후는 전부 기간 밖
                        token = None
                        break
                    got += 1
                    yield {"url": f"https://www.youtube.com/watch?v={vid}&lc={it['id']}",
                           "title": vtitle, "body": s.get("textOriginal", ""), "date": s["publishedAt"][:10]}
                else:
                    token = page.get("nextPageToken")
                if not token:
                    break
