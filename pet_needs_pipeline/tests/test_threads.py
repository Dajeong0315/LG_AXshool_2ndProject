"""스레드 수집·필터·퍼널·리포트 오프라인 테스트 (가짜 Apify 클라이언트, 크레딧 없음)."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
FAKE_TOKEN = "apify_api_FAKETOKEN123456"
os.environ["APIFY_TOKEN"] = FAKE_TOKEN

from analysis.threads_pilot import build_funnel, run_threads_analysis   # noqa: E402
from crawlers import threads_apify as ta                                 # noqa: E402
from crawlers.runner import load_raw                                     # noqa: E402


def ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT12:00:00.000Z")


def post(code, text, days=10, replies=1, **kw):
    return {"type": "post", "postId": f"id{code}", "code": code, "username": "SECRET_USER", "fullName": "SECRET NAME", "text": text,
            "likeCount": 3, "replyCount": replies, "repostCount": 1, "date": ago(days), "url": f"https://www.threads.com/t/{code}", **kw}


def reply(code, rid, text, days=5):
    return {"postUrl": f"https://www.threads.com/t/{code}", "replyId": rid, "replyUrl": f"https://www.threads.com/@x/post/{rid}",
            "authorUsername": "SECRET_USER", "text": text, "createdAt": ago(days), "likeCount": 1, "replyCount": 0}


class FakeClient:
    posts_by_kw, replies_by_code, calls = {}, {}, []

    def __init__(self, token):
        assert token == FAKE_TOKEN

    def actor(self, actor_id):
        outer = self

        class A:
            def call(self, run_input):
                FakeClient.calls.append((actor_id, run_input))
                if actor_id == ta.ACTOR_POSTS:
                    v = FakeClient.posts_by_kw[run_input["searchQueries"][0]]
                    if isinstance(v, Exception):
                        raise v
                    outer.ds = v
                else:
                    outer.ds = [r for u in run_input["postUrls"] for r in FakeClient.replies_by_code.get(ta.code_of(u), [])]
                return SimpleNamespace(default_dataset_id="ds")
        return A()

    def dataset(self, _):
        return SimpleNamespace(iterate_items=lambda: iter(self.ds))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = yaml.safe_load(Path("config/config.yaml").read_text("utf-8"))
        self.cfg["paths"].update(raw=str(self.tmp / "raw"), tables=str(self.tmp / "tables"), logs=str(self.tmp / "logs"))
        kw = pd.read_csv("config/threads_keywords.csv", encoding="utf-8-sig", dtype=str).fillna("")
        kw["판정"], kw["비고"] = "", ""                                    # 실제 확정 판정에 테스트가 영향받지 않게 초기화
        kw.to_csv(self.tmp / "kw.csv", index=False, encoding="utf-8-sig")
        self.cfg["threads"]["keywords_csv"] = str(self.tmp / "kw.csv")
        self.ad_re = re.compile("|".join(map(re.escape, self.cfg["ad_patterns"] + ta.THREADS_AD)))
        FakeClient.posts_by_kw, FakeClient.replies_by_code, FakeClient.calls = {}, {}, []

    def stat(self):
        return dict.fromkeys(ta.STAT_COLUMNS, 0) | {"keyword": "강아지 혼자", "query_group": "situation"}

    def run_it(self, only=None, mode="pilot", posts=None, replies=None, **kw):
        with mock.patch("apify_client.ApifyClient", FakeClient):
            return ta.run_threads(self.cfg, mode, posts, replies, only, **kw)


class TestFilters(Base):
    def test_filter_order_and_counts(self):
        items = [post("A", "우리 강아지가 혼자 있을 때 너무 짖어서 걱정이에요"),
                 post("B", "고양이는 혼자 있어도 잘 지내는데 정말 신기해요 집사라서"),       # 강아지 없음 + 고양이 전용
                 post("C", "회사 일이 너무 바빠서 요즘 정신이 하나도 없어요 진짜로"),          # 강아지 없음
                 post("D", "협찬 받은 강아지 펫캠 솔직 후기입니다 링크 클릭 해주세요"),           # 광고
                 post("E", "강아지 ㅠㅠ #강아지"),                                          # 길이 10자 미만
                 post("A", "우리 강아지가 혼자 있을 때 너무 짖어서 걱정이에요"),               # 중복 (같은 url)
                 post("F", "우리 강아지 오래된 글입니다 정말 오래됐어요", days=400),           # 기간 밖
                 post("G", "퍼피 교육 때문에 요즘 너무 고민이에요 혼자 두면 울어요"),          # 퍼피 → 강아지
                 {"type": "profile", "username": "x", "url": "https://www.threads.com/@x"}]    # 프로필 행 제외
        stat, seen = self.stat(), set()
        out = ta.filter_posts(items, "강아지 혼자", "situation", self.cfg, self.ad_re, seen, stat)
        self.assertEqual(sorted(p["item"]["code"] for p in out), ["A", "G"])
        self.assertEqual((stat["posts_raw"], stat["old_posts"], stat["nodog_posts"], stat["cat_only_posts"], stat["ad_posts"],
                          stat["short_posts"], stat["dup_posts"], stat["posts_final"]), (8, 1, 2, 1, 1, 1, 1, 2))
        self.assertEqual(stat["date_ok"], 8)

    def test_dog_words_include_breeds_and_puppy(self):
        t = ta.dog_terms(self.cfg)
        for s in ("푸들이 혼자 있어요", "퍼피 교육", "우리 애가 짖어요", "댕댕이 산책"):
            self.assertTrue(ta.has_dog(s, t), s)
        self.assertFalse(ta.has_dog("고양이만 키워요 집사입니다", t))

    def test_is_korean(self):
        self.assertTrue(ta.is_korean("강아지 혼자 있을 때 Tapo 홈캠"))
        self.assertFalse(ta.is_korean("my dog is alone at home all day"))
        self.assertFalse(ta.is_korean("犬が一人で留守番しています"))
        self.assertFalse(ta.is_korean("😀😀"))

    def test_replies_filter_parent_context_and_ids(self):
        p = post("A", "우리 강아지가 혼자 있을 때 너무 짖어서 걱정이에요", replies=3)
        stat, seen = self.stat(), set()
        posts = ta.filter_posts([p], "강아지 혼자", "situation", self.cfg, self.ad_re, seen, stat)
        prow, _ = ta.post_rows(posts, "강아지 혼자", "situation", "now")
        parents = ta.reply_targets(posts, prow)
        rs = [reply("A", "r1", "저희 집도 똑같아요 혼자 두면 계속 울어서 힘들어요"),          # 답글에 강아지 단어 없어도 부모 기준 통과
              reply("A", "r2", "협찬 제품 링크 클릭하면 할인코드 드려요 많이 이용해주세요"),       # 광고
              reply("A", "r3", "ㅠㅠ"),                                                     # 길이
              reply("A", "r4", "오래된 답글이지만 내용은 충분히 깁니다 정말로", days=400),       # 기간
              reply("A", "r1", "저희 집도 똑같아요 혼자 두면 계속 울어서 힘들어요"),          # 중복
              reply("ZZ", "r9", "부모 없는 답글은 버립니다 정말로 그렇습니다")]                # 부모 불명
        rows, metas = ta.filter_replies(rs, parents, self.cfg, self.ad_re, seen, stat, "now")
        self.assertEqual([r["url"] for r in rows], ["https://www.threads.com/t/A#rr1"])   # 답글 ID "r1" → #r + r1
        self.assertEqual((stat["replies_raw"], stat["old_replies"], stat["ad_replies"], stat["short_replies"], stat["dup_replies"],
                          stat["replies_final"]), (5, 1, 1, 1, 1, 1))
        self.assertEqual((rows[0]["feed_type"], rows[0]["body_from"], rows[0]["keyword"]), ("reply", "reply", "강아지 혼자"))
        blob = json.dumps(prow + rows + metas, ensure_ascii=False)
        self.assertNotIn("SECRET", blob)

    def test_reply_targets_only_posts_with_replies(self):
        posts = ta.filter_posts([post("A", "우리 강아지가 혼자 있을 때 너무 짖어요 걱정", replies=0), post("B", "우리 강아지 분리불안 때문에 너무 힘들어요", replies=2)],
                                "강아지 혼자", "situation", self.cfg, self.ad_re, set(), self.stat())
        prow, _ = ta.post_rows(posts, "강아지 혼자", "situation", "now")
        self.assertEqual(list(ta.reply_targets(posts, prow)), ["B"])

    def test_estimate_and_limits(self):
        self.assertEqual(ta.limits("pilot", None, None), (30, 20))
        self.assertEqual(ta.limits("full", None, None), (100, 20))
        self.assertEqual(ta.limits("pilot", 5, 3), (5, 3))
        pr = self.cfg["threads"]["price"]
        p, r, usd = ta.estimate(self.cfg, [(30, 20)] * 2)
        self.assertEqual((p, r), (60, 1200))
        self.assertAlmostEqual(usd, 2 * (pr["start_posts"] + 30 * pr["post"] + pr["start_replies"] + 600 * pr["reply"]))


class TestRun(Base):
    def setUp(self):
        super().setUp()
        FakeClient.posts_by_kw = {
            "강아지 혼자": [post("A", "우리 강아지가 혼자 있을 때 너무 짖어서 걱정이에요", replies=2), post("B", "고양이는 혼자 있어도 잘 지내요 집사라서 행복해요", replies=1)],
            "펫캠": RuntimeError(f"401 for https://api.apify.com/v2/x?token={FAKE_TOKEN}&key=abc123"),
            "강아지 홈캠": [post("A", "우리 강아지가 혼자 있을 때 너무 짖어서 걱정이에요")],             # 중복
        }
        FakeClient.replies_by_code = {"A": [reply("A", "r1", "저희 집도 똑같아요 혼자 두면 계속 울어서 힘들어요")], "B": [reply("B", "r2", "고양이 답글입니다 정말 좋아요 진짜로")]}

    def test_end_to_end_inputs_raw_failure_checkpoint_dedupe(self):
        self.assertTrue(self.run_it(["강아지 혼자", "펫캠", "강아지 홈캠"]))
        posts_call, reply_call = FakeClient.calls[0], FakeClient.calls[1]
        after = (datetime.now() - timedelta(days=self.cfg["period_days"])).strftime("%Y-%m-%d")
        self.assertEqual(posts_call[0], ta.ACTOR_POSTS)
        self.assertEqual(posts_call[1], {"mode": "search", "searchQueries": ["강아지 혼자"], "searchSort": "recent", "maxPosts": 30, "postedAfter": after})
        self.assertEqual(reply_call[0], ta.ACTOR_REPLIES)
        self.assertEqual(reply_call[1], {"postUrls": ["https://www.threads.com/t/A"], "maxRepliesPerPost": 20})    # 강아지 통과 게시물만
        raw = Path(self.cfg["paths"]["raw"])
        df = pd.read_csv(raw / "threads.csv", encoding="utf-8-sig")
        self.assertEqual(list(df.columns), ta.COLUMNS)
        self.assertEqual(sorted(df.feed_type), ["post", "reply"])
        f = raw / "threads_raw" / f"{datetime.now():%Y%m%d}_강아지 혼자.json"
        saved = json.loads(f.read_text("utf-8"))
        self.assertEqual((len(saved["posts"]), len(saved["replies"])), (2, 1))                                   # 원본 그대로 (작성자 필드 포함)
        self.assertEqual(saved["posts"][0]["username"], "SECRET_USER")
        self.assertFalse((raw / "threads_raw" / f"{datetime.now():%Y%m%d}_펫캠.json").exists())
        fails = (Path(self.cfg["paths"]["logs"]) / "failures.csv").read_text("utf-8-sig")
        self.assertIn("token=***", fails)
        self.assertNotIn(FAKE_TOKEN, fails)
        self.assertNotIn("abc123", fails)
        ck = json.loads((raw / "checkpoint.json").read_text("utf-8"))
        self.assertIn("threads_pilot|강아지 혼자", ck["exhausted"])
        self.assertNotIn("threads_pilot|펫캠", ck["exhausted"])
        stats = pd.read_csv(Path(self.cfg["paths"]["tables"]) / "threads_keyword_stats_pilot.csv", encoding="utf-8-sig").set_index("keyword")
        self.assertEqual(int(stats.loc["강아지 홈캠", "dup_posts"]), 1)
        FakeClient.calls.clear()                                              # 재실행: 실패 키워드만 다시
        FakeClient.posts_by_kw["펫캠"] = [post("Z", "펫캠으로 혼자 있는 강아지를 보면 걱정돼서 미안해요", replies=0)]
        self.run_it(["강아지 혼자", "펫캠"])
        self.assertEqual([c[1]["searchQueries"] for c in FakeClient.calls if "searchQueries" in c[1]], [["펫캠"]])

    def test_same_day_raw_is_kept_in_old(self):
        self.run_it(["강아지 혼자"])
        ta.reset_keywords(self.cfg, "pilot", ["강아지 혼자"])
        self.assertEqual(list((Path(self.cfg["paths"]["raw"]) / "threads_raw").glob("*강아지 혼자*.json")), [])
        self.assertEqual(len(list((Path(self.cfg["paths"]["raw"]) / "threads_raw" / "old").glob("*강아지 혼자*"))), 1)
        ck = json.loads((Path(self.cfg["paths"]["raw"]) / "checkpoint.json").read_text("utf-8"))
        self.assertNotIn("threads_pilot|강아지 혼자", ck["exhausted"])
        self.assertEqual(len(pd.read_csv(Path(self.cfg["paths"]["raw"]) / "threads.csv", encoding="utf-8-sig")), 0)
        FakeClient.calls.clear()
        self.run_it(["강아지 혼자"])                                             # 초기화 후 같은 글도 다시 수집 (중복 처리 안 됨)
        self.assertEqual(len(pd.read_csv(Path(self.cfg["paths"]["raw"]) / "threads.csv", encoding="utf-8-sig")), 2)

    def test_full_requires_keep_and_confirmation(self):
        kw = pd.read_csv(self.tmp / "kw.csv", encoding="utf-8-sig", dtype=str).fillna("")
        kw.loc[kw.keyword == "펫캠", "판정"] = "keep"
        kw.to_csv(self.tmp / "kw.csv", index=False, encoding="utf-8-sig")
        self.assertEqual(list(ta.select_keywords(self.cfg, "full")["keyword"]), ["펫캠"])
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(self.run_it(None, "full"))
        self.assertEqual(FakeClient.calls, [])                                # 거절하면 Apify 호출 0회
        kw.loc[kw.keyword == "노즈워크", "판정"] = "drop"
        kw.to_csv(self.tmp / "kw.csv", index=False, encoding="utf-8-sig")
        self.assertNotIn("노즈워크", set(ta.select_keywords(self.cfg, "pilot")["keyword"]))

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": ""}), mock.patch("dotenv.load_dotenv"):
            self.assertFalse(ta.run_threads(self.cfg, "pilot", 5, 5, ["펫캠"]))
        self.assertEqual(FakeClient.calls, [])

    def test_analysis_report_funnel_and_keywords_csv(self):
        FakeClient.posts_by_kw = {
            "펫캠": [post(f"P{i}", f"우리 강아지 펫캠으로 혼자 있을 때 보는데 {i}번째 글이에요 화면이 자꾸 끊겨서 불편해요" if i < 3 else f"우리 강아지 펫캠 {i}번째 후기 글입니다 만족해요", replies=0) for i in range(12)]
                   + [post("X", "협찬 받은 강아지 펫캠 후기입니다 링크 클릭")],
            "강아지 혼자": [post("Q", "우리 강아지가 혼자 있을 때 너무 짖어서 걱정이에요", replies=0)]}
        self.run_it(["펫캠", "강아지 혼자"])
        run_threads_analysis(self.cfg, "pilot")
        tables = Path(self.cfg["paths"]["tables"])
        rep = pd.read_csv(tables / "threads_pilot_report.csv", encoding="utf-8-sig").set_index("keyword")
        self.assertEqual(int(rep.loc["펫캠", "게시물 수(필터 전)"]), 13)
        self.assertEqual(int(rep.loc["펫캠", "게시물 수(필터 후)"]), 12)
        self.assertEqual(int(rep.loc["펫캠", "제거_광고"]), 1)
        self.assertAlmostEqual(rep.loc["펫캠", "불만 비율"], 3 / 12, places=3)            # substitute 만 불만 비율
        self.assertTrue(pd.isna(rep.loc["강아지 혼자", "불만 비율"]))
        self.assertEqual(rep.loc["펫캠", "판정 제안"], "keep 후보")
        self.assertEqual(rep.loc["강아지 혼자", "판정 제안"], "drop 후보")                # 게시물 10개 미만
        self.assertEqual(rep.loc["펫캠", "한국어 글 비율"], 1.0)
        self.assertTrue((tables / "threads_pilot_label_sample.csv").exists())
        kw = pd.read_csv(self.tmp / "kw.csv", encoding="utf-8-sig", dtype=str).fillna("").set_index("keyword")
        self.assertEqual((kw.loc["펫캠", "파일럿 게시물 수"], kw.loc["펫캠", "비고"]), ("12", "keep 후보"))
        self.assertTrue(kw.loc["펫캠", "자동 관련도"].endswith("%"))
        # 사람 판정은 유지
        kw2 = kw.reset_index()
        kw2.loc[kw2.keyword == "펫캠", ["판정", "비고"]] = ["keep", "사람 메모"]
        kw2.to_csv(self.tmp / "kw.csv", index=False, encoding="utf-8-sig")
        run_threads_analysis(self.cfg, "pilot")
        kw = pd.read_csv(self.tmp / "kw.csv", encoding="utf-8-sig", dtype=str).fillna("").set_index("keyword")
        self.assertEqual((kw.loc["펫캠", "판정"], kw.loc["펫캠", "비고"]), ("keep", "사람 메모"))

    def test_funnel_flags_zero_removal_steps(self):
        st = pd.DataFrame([dict.fromkeys(ta.STAT_COLUMNS, 0) | {"keyword": "k", "query_group": "situation", "posts_raw": 10, "posts_ko": 10,
                                                                "date_ok": 10, "posts_final": 10}])
        with self.assertLogs("pipeline", level="WARNING") as cm:
            f = build_funnel(st, self.cfg)
        posts = f[f["구분"] == "게시물"].set_index("단계")
        self.assertIn("⚠ 0건", posts.loc["강아지 관련(고양이 전용 제거)", "점검"])
        self.assertIn("강아지 단어 포함률 100.0%", posts.loc["강아지 관련(고양이 전용 제거)", "점검"])
        self.assertIn("날짜 파싱 성공 10/10", posts.loc["기간(최근 365일)", "점검"])
        self.assertTrue(any("강아지 관련" in m for m in cm.output))


class TestCorpus(Base):
    def test_corpus_sources_excludes_instagram_and_threads(self):
        raw = Path(self.cfg["paths"]["raw"])
        raw.mkdir(parents=True)
        for name in ("naver_blog", "youtube", "instagram", "threads"):
            pd.DataFrame([dict(source=name, url=f"u_{name}", url_hash=name, title="", body="본문", date="2026-09-01", keyword="k",
                               feed_type="post", rating="", body_from="page")]).to_csv(raw / f"{name}.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame([dict(url_hash="x", like_count=1)]).to_csv(raw / "threads_meta.csv", index=False)
        self.assertEqual(set(load_raw({**self.cfg, "corpus": {}})["source"]), {"naver_blog", "youtube", "instagram", "threads"})      # 옛 동작
        self.assertEqual(set(load_raw(self.cfg)["source"]), {"naver_blog", "youtube"})                                               # 기본 설정
        self.cfg["corpus"]["sources"].append("threads")                                                                               # 편입하려면 목록에 추가
        self.assertEqual(set(load_raw(self.cfg)["source"]), {"naver_blog", "youtube", "threads"})


if __name__ == "__main__":
    unittest.main()
