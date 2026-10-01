"""인스타 수집·필터·라벨링 오프라인 테스트 (Apify 호출·크레딧 없음: 가짜 클라이언트 사용).

실행:  python -m unittest discover -s tests -v      (pet_needs_pipeline 폴더에서)
"""
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
os.environ["APIFY_TOKEN"] = FAKE_TOKEN                     # 실제 .env 토큰이 쓰이지 않게 선점

from analysis.ig_label_eval import label_precision      # noqa: E402
from analysis.ig_pilot import match_reaction, run_ig_analysis  # noqa: E402
from crawlers import instagram_apify as ia               # noqa: E402
from crawlers.runner import load_raw                     # noqa: E402


def ago(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT12:00:00.000Z")


def post(code, cap, days=10, comments=1, typ="Image"):
    return {"url": f"https://www.instagram.com/p/{code}/", "shortCode": code, "caption": cap, "timestamp": ago(days),
            "commentsCount": comments, "likesCount": 3, "type": typ, "ownerUsername": "SECRET_USER", "ownerFullName": "SECRET NAME"}


def comment(code, cid, text):
    return {"id": cid, "postUrl": f"https://www.instagram.com/p/{code}/", "text": text, "timestamp": ago(5), "likesCount": 1,
            "ownerUsername": "SECRET_USER", "ownerProfilePicUrl": "http://x/SECRET.jpg"}


class FakeClient:
    """ApifyClient 대역. posts_by_tag: 태그 → 게시물 목록 / 예외. calls 로 호출 기록."""
    posts_by_tag, comments_by_code, calls = {}, {}, []

    def __init__(self, token):
        assert token == FAKE_TOKEN

    def actor(self, actor_id):
        outer = self

        class A:
            def call(self, run_input):
                FakeClient.calls.append((actor_id, run_input))
                if "hashtags" in run_input:
                    v = FakeClient.posts_by_tag[run_input["hashtags"][0]]
                    if isinstance(v, Exception):
                        raise v
                    outer.ds = v
                else:
                    outer.ds = [c for u in run_input["directUrls"] for c in FakeClient.comments_by_code.get(ia.shortcode(u), [])]
                return SimpleNamespace(default_dataset_id="ds")
        return A()

    def dataset(self, _):
        return SimpleNamespace(iterate_items=lambda: iter(self.ds))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = yaml.safe_load(Path("config/config.yaml").read_text("utf-8"))
        self.cfg["instagram"].pop("period_days", None)                       # 인스타 전용 기간(실제 설정)에 테스트가 영향받지 않게 전역 365일 기준으로 고정
        self.cfg["paths"].update(raw=str(self.tmp / "raw"), tables=str(self.tmp / "tables"), logs=str(self.tmp / "logs"),
                                 ig_tags=str(self.tmp / "tags.csv"))
        shutil.copy("config/hashtags_pilot_v2.csv", self.tmp / "tags.csv")
        tg = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str).fillna("")
        tg["판정"], tg["비고"] = "", ""                                    # 실제 확정 판정(drop)에 테스트가 영향받지 않게 초기화
        tg.to_csv(self.tmp / "tags.csv", index=False, encoding="utf-8-sig")
        self.ad_re = re.compile("|".join(map(re.escape, self.cfg["ad_patterns"] + ia.EXTRA_AD)))
        FakeClient.posts_by_tag, FakeClient.comments_by_code, FakeClient.calls = {}, {}, []

    def stat(self):
        return dict.fromkeys(ia.STAT_COLUMNS, 0) | {"tag": "t", "query_group": "robot"}


class TestFilters(Base):
    def test_dog_word_ignores_hashtags_and_quantities(self):
        self.assertFalse(ia.has_dog(ia.TAG_RE.sub(" ", "#로봇청소기강아지 청소기 좋아요")))
        self.assertTrue(ia.has_dog(ia.TAG_RE.sub(" ", "우리 강아지가 도망가요 #로청")))
        self.assertTrue(ia.has_dog("우리 애가 짖어요"))
        self.assertFalse(ia.has_dog("청소기 3개 샀어요, 몇 개 더"))
        self.assertTrue(ia.has_dog("개가 무서워해요"))

    def test_animal_dog_terms_merged_for_all_robot_tags(self):
        terms = self.cfg["animal"]["dog_terms"]
        self.assertFalse(ia.has_dog("골든리트리버 가족", ()))                     # 기본 단어만으로는 못 잡던 글
        self.assertTrue(ia.has_dog("포메라니안이 로봇청소기를 봐요", terms))
        self.assertTrue(ia.has_dog("우리집 견주는 저예요", terms))
        self.assertFalse(ia.has_dog("로봇청소기 청소 잘 돼요", terms))
        caps = ["비숑이 로봇청소기를 보고 짖어요 진짜로 #로봇청소기", "로봇청소기 청소 잘 돼요 진짜로 좋아요 #강아지"]
        for tag in ("로봇청소기", "로봇청소기강아지"):                              # 두 태그 모두 같은 사전
            stat = self.stat()
            ia.select_posts([post("A", caps[0]), post("B", caps[1])], tag, "robot", self.cfg, self.ad_re, set(), stat, [])
            self.assertEqual(stat["nonad_dog"], 2, tag)                                   # 두 태그 같은 사전·같은 범위
        self.cfg["instagram"]["use_animal_dog_terms"] = False
        stat = self.stat()
        ia.select_posts([post("A", caps[0])], "로봇청소기강아지", "robot", self.cfg, self.ad_re, set(), stat, [])
        self.assertEqual(stat["nonad_dog"], 0)

    def test_post_filter_order_and_counts(self):
        items = [post("A", "우리 강아지가 로봇청소기를 보고 짖어요 진짜로"),
                 post("B", "협찬 받은 로봇청소기 강아지 후기입니다 좋아요"),
                 post("C", "#로봇청소기 #강아지 #댕댕이"),
                 post("A", "우리 강아지가 로봇청소기를 보고 짖어요 진짜로"),
                 post("D", "로봇청소기 청소 잘 되네요 추천합니다 #청소기추천"),
                 post("E", "우리 강아지 오래된 게시물입니다 아주 오래됨", days=400)]
        stat, removed, seen = self.stat(), [], set()
        out = ia.select_posts(items, "로청강아지_x", "robot", self.cfg, self.ad_re, seen, stat, removed)
        self.assertEqual([p["url"][-2] for p in out], ["A"])
        self.assertEqual((stat["old_posts"], stat["ad_posts"], stat["hashonly_posts"], stat["dup_posts"], stat["nodog_posts"]),
                         (1, 1, 1, 1, 1))
        self.assertEqual([r["url"][-2] for r in removed], ["D"])         # 태그만 강아지 → 제거 샘플에 기록

    def test_period_expansion_only_for_reaction_tags(self):
        items = [post(f"P{i}", f"우리 강아지 로봇청소기 반응 게시물 {i}번째", days=500) for i in range(3)]
        for tag, group, days in [("로봇청소기강아지", "robot", 730), ("제트봇", "robot", 365), ("펫캠", "substitute", 365)]:
            stat = self.stat()
            posts = ia.select_posts(items, tag, group, self.cfg, self.ad_re, set(), stat, [])
            self.assertEqual(stat["period_days"], days, tag)
            self.assertEqual(len(posts), 3 if days == 730 else 0, tag)

    def test_instagram_period_overrides_global(self):
        self.cfg["instagram"]["period_days"] = 1095
        items = [post(f"P{i}", f"우리 강아지가 펫캠 보고 반응했어요 {i}번째", days=800) for i in range(3)]
        stat = self.stat()
        posts = ia.select_posts(items, "펫캠", "substitute", self.cfg, self.ad_re, set(), stat, [])
        self.assertEqual((stat["period_days"], len(posts)), (1095, 3))     # 전역 365일이면 0건

    def test_no_expansion_when_enough_posts(self):
        items = [post(f"P{i}", f"우리 강아지 로봇청소기 반응 게시물 {i}번째", days=30) for i in range(10)]
        stat = self.stat()
        ia.select_posts(items, "로봇청소기강아지", "robot", self.cfg, self.ad_re, set(), stat, [])
        self.assertEqual(stat["period_days"], 365)

    def test_comment_filters_and_no_pii(self):
        p = post("A", "우리 강아지가 로봇청소기를 보고 짖어요 진짜로")
        stat, seen = self.stat(), set()
        posts = ia.select_posts([p], "t", "robot", self.cfg, self.ad_re, seen, stat, [])
        prow, _ = ia.post_rows(posts, "t", "robot", "now")
        parents = ia.comment_targets(posts, prow)
        cs = [comment("A", "1", "우리 애도 처음엔 무서워했는데 이제는 익숙해졌어요 @friend"), comment("A", "2", "ㅋㅋ 귀여워요 😍 #강아지"),
              comment("A", "3", "협찬 광고 제품이라서 좋아요 정말로 그렇습니다")]
        rows, metas = ia.comment_rows(cs, parents, self.ad_re, seen, stat, "now")
        self.assertEqual((stat["short_comments"], stat["ad_comments"], len(rows)), (1, 1, 1))
        self.assertNotIn("@friend", rows[0]["body"])
        self.assertTrue(rows[0]["url"].endswith("/#c1"))
        blob = json.dumps(prow + rows + metas, ensure_ascii=False)
        for secret in ("SECRET", "friend"):
            self.assertNotIn(secret, blob)


class TestIntersectionTag(Base):
    """#로봇청소기 → 강아지 필터(교집합 방식)."""
    TAG = "로봇청소기"

    def items(self):
        return [post("A", "#로봇청소기 #강아지 오늘도 청소를 잘 하네요 진짜로요"),              # 다른 해시태그의 강아지 → 통과
                post("B", "#로봇청소기 청소를 정말 잘 하네요 진짜로 좋아요 추천해요"),           # 강아지 없음 → 제거
                post("C", "#로봇청소기#포메 조용해서 정말 좋아요 다들 써보세요 #댕댕이"),       # 붙어 있는 태그, #댕댕이 → 통과
                post("D", "#로봇청소기추천 청소를 정말 잘 하네요 진짜로 좋아요 추천해요"),       # 다른 태그(접두 일치)는 지우지 않음, 강아지 없음
                post("E", "우리 강아지가 청소기를 보고 짖어요 #로봇청소기", days=400),           # 본문 강아지지만 1년 밖
                post("F", "협찬 제품 강아지 털 청소 잘 됩니다 #로봇청소기")]                    # 광고

    def test_scope_and_metrics(self):
        stat, removed = self.stat(), []
        posts = ia.select_posts(self.items(), self.TAG, "robot", self.cfg, self.ad_re, set(), stat, removed)
        self.assertEqual(sorted(p["url"][-2] for p in posts), ["A", "C"])
        self.assertEqual(stat["period_days"], 365)                                        # 확장 대상 아님
        self.assertEqual((stat["posts_raw"], stat["ad_all"], stat["nonad_dog"], stat["recent_all"]), (6, 1, 3, 5))
        self.assertEqual(sorted(r["url"][-2] for r in removed), ["B", "D"])
        stat2 = self.stat()                                                               # 모든 robot 태그가 같은 범위(해시태그+본문)
        ia.select_posts([post("A", "#로봇청소기 #강아지 오늘도 청소를 잘 하네요 진짜로요")], "제트봇", "robot", self.cfg, self.ad_re, set(), stat2, [])
        self.assertEqual(stat2["nonad_dog"], 1)

    def test_search_tag_variants_are_not_dog_evidence(self):
        caps = ["#로봇청소기강아지 청소가 정말 잘 되네요 진짜로 좋아요", "#로봇청소기_강아지 청소가 정말 잘 되네요 진짜로 좋아요",
                "#로청강아지 #강아지로봇청소기 청소가 정말 잘 되네요 진짜", "#로봇청소기 #댕댕이 청소가 정말 잘 되네요 진짜로 좋아요"]
        for tag in ("로봇청소기", "로봇청소기강아지", "제트봇"):
            stat = self.stat()
            ia.select_posts([post(f"P{i}", c) for i, c in enumerate(caps)], tag, "robot", self.cfg, self.ad_re, set(), stat, [])
            self.assertEqual(stat["nonad_dog"], 1, tag)                                   # #댕댕이 만 인정

    def test_collect_limits_and_comments_only_for_dog_posts(self):
        FakeClient.posts_by_tag = {self.TAG: self.items()[:4]}
        FakeClient.comments_by_code = {c: [comment(c, "1", "우리 애도 처음엔 무서워했는데 이제는 익숙해졌어요")] for c in "ABCD"}
        with mock.patch("apify_client.ApifyClient", FakeClient):
            ia.run_instagram(self.cfg, "pilot", None, None, [self.TAG])
        posts_call, comment_call = FakeClient.calls
        self.assertEqual(posts_call[1]["resultsLimit"], 50)                               # config tag_overrides
        self.assertEqual(comment_call[1]["resultsLimit"], 10)
        self.assertEqual(sorted(u[-2] for u in comment_call[1]["directUrls"]), ["A", "C"])   # 강아지 통과 게시물만
        self.assertNotIn("sort", json.dumps(posts_call[1]))                               # 액터에 없는 필드는 보내지 않음
        stats = pd.read_csv(Path(self.cfg["paths"]["tables"]) / "ig_tag_stats_pilot.csv", encoding="utf-8-sig").iloc[0]
        pr = self.cfg["instagram"]["price_per_1000"]
        self.assertAlmostEqual(stats.cost_usd, round(4 * pr["posts"] / 1000 + 2 * pr["comments"] / 1000, 4))

    def clear_human(self):
        df = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str).fillna("")
        df.loc[df.tag == self.TAG, ["판정", "비고"]] = ""
        df.to_csv(self.tmp / "tags.csv", index=False, encoding="utf-8-sig")

    def run_analysis(self, n_dog, n_other):
        self.clear_human()
        items = [post(f"D{i}", f"#로봇청소기 #강아지 청소기 후기 {i}번째 글입니다 좋아요") for i in range(n_dog)] + \
                [post(f"O{i}", f"#로봇청소기 청소기 후기 {i}번째 글입니다 정말 좋아요") for i in range(n_other)]
        FakeClient.posts_by_tag = {self.TAG: items, "로봇청소기강아지": [post("Z", "우리 강아지가 청소기를 보고 짖어요 진짜로")]}
        FakeClient.comments_by_code = {}
        with mock.patch("apify_client.ApifyClient", FakeClient):
            ia.run_instagram(self.cfg, "pilot", None, None, [self.TAG, "로봇청소기강아지"])
        run_ig_analysis(self.cfg, "pilot")
        rep = pd.read_csv(Path(self.cfg["paths"]["tables"]) / "ig_pilot_report.csv", encoding="utf-8-sig").set_index("tag")
        note = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str).fillna("").set_index("tag")["비고"]
        return rep.loc[self.TAG], note[self.TAG]

    def test_verdict_hit_rate_threshold(self):
        r, note = self.run_analysis(5, 45)                                                # 적중률 10% → 본수집 후보
        self.assertAlmostEqual(r["강아지 적중률"], 0.1)
        self.assertTrue(note.startswith("본수집 후보"), note)
        self.assertAlmostEqual(r["광고 비율"], 0.0)
        self.assertAlmostEqual(r["최근 1년 게시물 비율"], 1.0)
        self.assertAlmostEqual(r["강아지 통과 게시물 1개당 비용($)"], 50 * self.cfg["instagram"]["price_per_1000"]["posts"] / 1000 / 5, places=4)

    def test_verdict_below_threshold_and_compare_table(self):
        r, note = self.run_analysis(4, 46)                                                # 8% → drop 후보
        self.assertTrue(note.startswith("drop 후보"), note)
        cmp = pd.read_csv(Path(self.cfg["paths"]["tables"]) / "ig_intersection_compare.csv", encoding="utf-8-sig").set_index("항목")
        self.assertEqual(list(cmp.columns), [self.TAG, "로봇청소기강아지"])
        for row in ("수집 게시물 수", "최근 1년 게시물 수", "강아지 글 수(광고 제외·강아지 판별 통과)", "비용(추정,$)"):
            self.assertIn(row, cmp.index)
        self.assertEqual(int(cmp.loc["수집 게시물 수", self.TAG]), 50)

    def test_human_decision_is_kept(self):
        df = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str).fillna("")
        df.loc[df.tag == self.TAG, ["판정", "비고"]] = ["drop", "교집합 방식 검증: 사람이 쓴 메모"]
        df.to_csv(self.tmp / "tags.csv", index=False, encoding="utf-8-sig")
        FakeClient.posts_by_tag = {self.TAG: [post("D0", "#로봇청소기 #강아지 청소기 후기 글입니다 정말 좋아요")], "로봇청소기강아지": []}
        with mock.patch("apify_client.ApifyClient", FakeClient):
            ia.run_instagram(self.cfg, "pilot", 5, 5, ["로봇청소기강아지"])          # drop 태그는 --tags 로 지정해도 수집 안 함
        self.assertEqual([c[1]["hashtags"] for c in FakeClient.calls], [["로봇청소기강아지"]])
        FakeClient.calls.clear()
        with mock.patch("apify_client.ApifyClient", FakeClient):
            self.assertFalse(ia.run_instagram(self.cfg, "pilot", 5, 5, [self.TAG]))
        self.assertEqual(FakeClient.calls, [])
        # 분석을 돌려도 사람이 쓴 판정·비고는 그대로
        Path(self.cfg["paths"]["tables"]).mkdir(parents=True, exist_ok=True)
        pd.DataFrame([dict.fromkeys(ia.STAT_COLUMNS, 0) | {"tag": self.TAG, "query_group": "robot", "posts_raw": 1, "posts_final": 1,
                                                          "nonad_dog": 1, "period_days": 365}]).to_csv(
            Path(self.cfg["paths"]["tables"]) / "ig_tag_stats_pilot.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame([dict(source="instagram", url="u", url_hash="h", title="", body="강아지 후기", date="2026-09-01", keyword=self.TAG,
                           feed_type="post", rating="", body_from="caption", query_group="robot", collected_at="x")]).to_csv(
            Path(self.cfg["paths"]["raw"]) / "instagram.csv", index=False, encoding="utf-8-sig")
        run_ig_analysis(self.cfg, "pilot")
        row = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str).fillna("").set_index("tag").loc[self.TAG]
        self.assertEqual((row["판정"], row["비고"]), ("drop", "교집합 방식 검증: 사람이 쓴 메모"))

    def test_compare_table_is_frozen(self):
        self.run_analysis(5, 45)
        f = Path(self.cfg["paths"]["tables"]) / "ig_intersection_compare.csv"
        before = f.read_bytes()
        self.run_analysis(1, 49)                                                          # 값이 달라져도
        self.assertEqual(f.read_bytes(), before)                                          # compare_frozen 이면 그대로

    def test_tag_row_registered(self):
        df = pd.read_csv("config/hashtags_pilot_v2.csv", encoding="utf-8-sig", dtype=str).fillna("").set_index("tag")
        self.assertEqual(df.loc[self.TAG, "query_group"], "robot")
        self.assertEqual(df.loc[self.TAG, "수집 여부"], "수집")
        self.assertIn("교집합 방식", df.loc[self.TAG, "선정 이유"])


class TestReaction(Base):
    def test_negation_and_multilabel(self):
        r = yaml.safe_load(Path("config/ig_reaction_dict.yaml").read_text("utf-8"))
        self.assertEqual(match_reaction("안 무서워해요", r), {})
        self.assertEqual(match_reaction("짖지도 않아요", r), {})
        self.assertEqual(set(match_reaction("처음엔 무서워했는데 이제는 익숙해요", r)), {"fear", "adapted"})
        self.assertEqual(set(match_reaction("관심 없어요", r)), {"indifferent"})

    def test_precision(self):
        df = pd.DataFrame({"rule_label": ["fear", "fear", "play"], "human_label": ["fear", "curious", "play|fear"]})
        p = label_precision(df).set_index("rule_label")["precision"]
        self.assertEqual((p["fear"], p["play"]), (0.5, 1.0))


class TestRun(Base):
    def setUp(self):
        super().setUp()
        FakeClient.posts_by_tag = {
            "로봇청소기강아지": [post("A", "우리 강아지가 로봇청소기를 보고 짖어요 진짜로", comments=2), post("B", "협찬 강아지 로봇청소기 후기 입니다 좋아요")],
            "펫캠": RuntimeError(f"401 for https://api.apify.com/v2/x?token={FAKE_TOKEN}&key=abc123"),
            "강아지cctv": [post("A", "우리 강아지가 로봇청소기를 보고 짖어요 진짜로")],           # 위 태그와 중복
        }
        FakeClient.comments_by_code = {"A": [comment("A", "1", "우리 애도 처음엔 무서워했는데 이제는 익숙해졌어요")]}

    def run_it(self, tags, mode="pilot", **kw):
        with mock.patch("apify_client.ApifyClient", FakeClient):
            return ia.run_instagram(self.cfg, mode, 10, 10, tags, **kw)

    def test_end_to_end_failure_masking_checkpoint_dedupe(self):
        self.assertTrue(self.run_it(["로봇청소기강아지", "펫캠", "강아지cctv"]))
        raw = Path(self.cfg["paths"]["raw"])
        df = pd.read_csv(raw / "instagram.csv", encoding="utf-8-sig")
        self.assertEqual(list(df.columns), ia.COLUMNS)
        self.assertEqual(sorted(df.feed_type), ["comment", "post"])
        self.assertEqual(len(list((raw / "instagram_raw").glob("*_posts.json"))), 2)     # 실패 태그는 raw 없음
        fails = (Path(self.cfg["paths"]["logs"]) / "failures.csv").read_text("utf-8-sig")
        self.assertIn("token=***", fails)
        self.assertNotIn(FAKE_TOKEN, fails)
        self.assertNotIn("abc123", fails)
        ck = json.loads((raw / "checkpoint.json").read_text("utf-8"))
        self.assertIn("instagram_pilot|로봇청소기강아지", ck["exhausted"])
        self.assertNotIn("instagram_pilot|펫캠", ck["exhausted"])                            # 실패 태그는 재시도 대상
        stats = pd.read_csv(Path(self.cfg["paths"]["tables"]) / "ig_tag_stats_pilot.csv", encoding="utf-8-sig")
        self.assertEqual(int(stats.set_index("tag").loc["강아지cctv", "dup_posts"]), 1)
        # 재실행: 완료 태그는 건너뛰고 실패 태그만 다시 호출
        FakeClient.calls.clear()
        FakeClient.posts_by_tag["펫캠"] = [post("Z", "펫캠으로 혼자 있는 강아지를 보면 걱정돼요 미안해요")]
        self.run_it(["로봇청소기강아지", "펫캠"])
        self.assertEqual([c[1]["hashtags"] for c in FakeClient.calls if "hashtags" in c[1]], [["펫캠"]])

    def test_comment_actor_skipped_when_no_comments(self):
        FakeClient.posts_by_tag = {"로봇청소기강아지": [post("A", "우리 강아지가 로봇청소기를 보고 짖어요 진짜로", comments=0)]}
        self.run_it(["로봇청소기강아지"])
        self.assertEqual([c[0] for c in FakeClient.calls], [ia.POST_ACTOR])

    def test_full_requires_confirmation(self):
        tags = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str)
        tags.loc[tags.tag == "펫캠", "판정"] = "keep"
        tags.to_csv(self.tmp / "tags.csv", index=False, encoding="utf-8-sig")
        with mock.patch("builtins.input", return_value="n"):
            self.assertFalse(self.run_it(None, "full"))
        self.assertEqual(FakeClient.calls, [])                       # 거절하면 Apify 호출 0회
        self.assertEqual(list(ia.select_tags(self.cfg, "full")["tag"]), ["펫캠"])     # keep 만
        all_tags = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str)
        active = all_tags[(all_tags["수집 여부"] == "수집") & (all_tags["판정"].fillna("").str.lower() != "drop")]
        self.assertEqual(len(ia.select_tags(self.cfg, "pilot")), len(active))              # 제외 태그·drop 확정 태그 미포함
        self.assertEqual(len(ia.select_tags(self.cfg, "pilot", include_dropped=True)), int((all_tags["수집 여부"] == "수집").sum()))
        self.assertNotIn("노즈워크", set(ia.select_tags(self.cfg, "pilot")["tag"]))
        pr = self.cfg["instagram"]["price_per_1000"]
        self.assertAlmostEqual(ia.estimate(self.cfg, [(100, 30)] * 10)[2], 1000 * pr["posts"] / 1000 + 30000 * pr["comments"] / 1000)

    def test_missing_token(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": ""}), mock.patch("dotenv.load_dotenv"):
            self.assertFalse(ia.run_instagram(self.cfg, "pilot", 10, 10, ["펫캠"]))
        self.assertEqual(FakeClient.calls, [])

    def test_reprocess_and_analysis(self):
        self.run_it(["로봇청소기강아지", "강아지cctv"])
        raw, tables = Path(self.cfg["paths"]["raw"]), Path(self.cfg["paths"]["tables"])
        ck_before = (raw / "checkpoint.json").read_bytes()
        run_ig_analysis(self.cfg, "pilot")                           # v1 결과 생성
        v1 = (raw / "instagram.csv").read_bytes()
        FakeClient.calls.clear()
        self.cfg["period_days"] = 5                                  # 기간을 좁혀 결과가 달라지게
        ia.reprocess_instagram(self.cfg, "pilot")
        run_ig_analysis(self.cfg, "pilot")
        self.assertEqual(FakeClient.calls, [])                       # Apify 호출 없음
        self.assertEqual((raw / "checkpoint.json").read_bytes(), ck_before)
        self.assertEqual((raw / "v1" / "instagram.csv").read_bytes(), v1)
        self.assertTrue((tables / "ig_funnel_v1.csv").exists() and (tables / "ig_filter_change_compare.csv").exists())
        self.assertTrue((Path(self.cfg["paths"]["ig_tags"]).with_name("hashtags_pilot_v2_v1result.csv")).exists())
        # 두 번째 재처리에도 v1 은 최초 상태 유지
        self.cfg["period_days"] = 365
        ia.reprocess_instagram(self.cfg, "pilot")
        self.assertEqual((raw / "v1" / "instagram.csv").read_bytes(), v1)
        for f in ("ig_funnel", "ig_pilot_report", "ig_reaction_summary", "ig_substitute_emotion", "ig_pilot_label_sample"):
            self.assertTrue((tables / f"{f}.csv").exists(), f)

    def test_reset_tags_for_recollect(self):
        FakeClient.posts_by_tag["펫캠"] = [post("Z", "펫캠으로 혼자 있는 강아지를 보면 걱정돼요 미안해요", comments=1)]
        FakeClient.comments_by_code = {"Z": [comment("Z", "9", "우리 집 강아지도 펫캠으로 보면 귀여워요 정말로")],
                                       "A": [comment("A", "1", "우리 애도 처음엔 무서워했는데 이제는 익숙해졌어요")]}
        self.run_it(["로봇청소기강아지", "펫캠"])
        raw = Path(self.cfg["paths"]["raw"])
        ck0 = json.loads((raw / "checkpoint.json").read_text("utf-8"))
        ia.reset_tags(self.cfg, "pilot", ["펫캠"])
        df = pd.read_csv(raw / "instagram.csv", encoding="utf-8-sig")
        self.assertEqual(set(df.keyword), {"로봇청소기강아지"})                                # 다른 태그 행은 그대로
        self.assertEqual(set(pd.read_csv(raw / "instagram_meta.csv")["url_hash"]), set(df.url_hash))
        old = list((raw / "instagram_raw" / "old").glob("*펫캠*"))
        self.assertEqual(len(old), 2)                                                     # 원본은 삭제가 아니라 보관
        self.assertEqual(list((raw / "instagram_raw").glob("*펫캠*")), [])
        ck = json.loads((raw / "checkpoint.json").read_text("utf-8"))
        self.assertNotIn("instagram_pilot|펫캠", ck["done"])
        self.assertNotIn("instagram_pilot|펫캠", ck["exhausted"])
        self.assertEqual(ck["done"]["instagram_pilot|로봇청소기강아지"], ck0["done"]["instagram_pilot|로봇청소기강아지"])
        FakeClient.calls.clear()                                                          # 같은 게시물도 중복 처리되지 않고 다시 수집
        self.run_it(["로봇청소기강아지", "펫캠"])
        self.assertEqual([c[1]["hashtags"] for c in FakeClient.calls if "hashtags" in c[1]], [["펫캠"]])
        self.assertIn("펫캠", set(pd.read_csv(raw / "instagram.csv", encoding="utf-8-sig").keyword))

    def test_estimate_excludes_finished_tags(self):
        self.run_it(["로봇청소기강아지"])
        FakeClient.calls.clear()
        with mock.patch("builtins.print") as pr, mock.patch("apify_client.ApifyClient", FakeClient):
            ia.run_instagram(self.cfg, "pilot", 30, 10, ["로봇청소기강아지", "펫캠"])
        line = " ".join(str(a) for c in pr.call_args_list for a in c.args)
        self.assertIn("새로 수집할 태그 1개 (펫캠)", line)
        self.assertIn("게시물 최대 30건, 댓글 최대 300건", line)

    def test_save_raw_never_overwrites_same_day_file(self):
        raw = Path(self.cfg["paths"]["raw"])
        ia.save_raw(raw, "펫캠", "posts", [{"id": 1}])
        ia.save_raw(raw, "펫캠", "posts", [{"id": 2}])                                    # 같은 날 재수집
        cur = list((raw / "instagram_raw").glob("*_펫캠_posts.json"))
        self.assertEqual(json.loads(cur[0].read_text("utf-8")), [{"id": 2}])
        self.assertEqual(json.loads(next((raw / "instagram_raw" / "old").glob("*펫캠*")).read_text("utf-8")), [{"id": 1}])

    def test_reprocess_refuses_full_mode(self):
        self.run_it(["로봇청소기강아지"])
        raw = Path(self.cfg["paths"]["raw"])
        before = (raw / "instagram.csv").read_bytes()
        self.assertFalse(ia.reprocess_instagram(self.cfg, "full"))
        self.assertEqual((raw / "instagram.csv").read_bytes(), before)

    def test_full_run_keeps_pilot_outputs_and_writes_full_outputs(self):
        FakeClient.posts_by_tag = {"펫캠": [post("P1", "펫캠으로 혼자 있는 강아지를 보면 걱정돼요 미안해요", comments=1)]}
        FakeClient.comments_by_code = {"P1": [comment("P1", "1", "출근할 때 불안해 보여서 펫캠으로 자주 확인해요")]}
        self.run_it(["펫캠"])
        run_ig_analysis(self.cfg, "pilot")
        tables = Path(self.cfg["paths"]["tables"])
        pilot_files = ["ig_funnel.csv", "ig_pilot_report.csv", "ig_substitute_emotion.csv", "ig_pilot_label_sample.csv"]
        snap = {f: (tables / f).read_bytes() for f in pilot_files}
        tg = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str).fillna("")
        tg.loc[tg.tag == "펫캠", ["판정", "비고"]] = ["keep", "본수집 확정"]
        tg.to_csv(self.tmp / "tags.csv", index=False, encoding="utf-8-sig")
        tag_snap = (self.tmp / "tags.csv").read_bytes()
        FakeClient.posts_by_tag = {"펫캠": [post("P1", "펫캠으로 혼자 있는 강아지를 보면 걱정돼요 미안해요", comments=1),      # 파일럿과 중복
                                             post("P2", "강아지가 혼자 있을 때 안심되고 다행이라 생각해요 정말로", comments=1)]}
        FakeClient.comments_by_code = {"P2": [comment("P2", "7", "우리 강아지도 혼자 있을 때 걱정돼서 샀어요 정말로")]}
        FakeClient.calls.clear()
        self.run_it(None, "full", assume_yes=True)
        self.assertEqual([ia.shortcode(u) for c in FakeClient.calls if "directUrls" in c[1] for u in c[1]["directUrls"]], ["P2"])   # 중복 게시물 댓글은 재수집 안 함
        run_ig_analysis(self.cfg, "full")
        for f in pilot_files:
            self.assertEqual((tables / f).read_bytes(), snap[f], f)                       # 파일럿 결과 보존
        self.assertEqual((self.tmp / "tags.csv").read_bytes(), tag_snap)                  # 파일럿 컬럼도 본수집이 건드리지 않음
        for f in ("ig_funnel_full.csv", "ig_report_full.csv", "ig_substitute_emotion_full.csv", "ig_emotion_rank_change_full.csv",
                  "ig_source_decision.csv"):
            self.assertTrue((tables / f).exists(), f)
        fun = pd.read_csv(tables / "ig_funnel_full.csv").set_index("단계")["문서 수"]
        self.assertEqual(int(fun["[게시물] 수집 원본"]), 2)
        self.assertEqual(int(fun["[게시물] 중복 제거 후"]), 1)                              # 파일럿과 겹친 1건

    def test_emotion_rank_change_and_source_decision(self):
        from analysis.ig_pilot import emotion_rank_change
        a = pd.DataFrame({"emotion_word": ["불안", "걱정", "안심"], "doc_count": [10, 8, 7], "ratio": [0.10, 0.08, 0.07]})
        b = pd.DataFrame({"emotion_word": ["불안", "걱정", "안심"], "doc_count": [5, 9, 1], "ratio": [0.05, 0.09, 0.01]})
        c = emotion_rank_change(a, b).set_index("emotion_word")
        self.assertEqual((c.loc["걱정", "pilot_rank"], c.loc["걱정", "full_rank"], c.loc["걱정", "rank_change"]), (2, 1, 1))
        self.assertEqual(c.loc["불안", "rank_change"], -1)
        FakeClient.posts_by_tag = {"펫캠": [post("P1", "펫캠으로 혼자 있는 강아지를 보면 걱정돼요 미안해요", comments=0)]}
        self.run_it(["펫캠"])
        run_ig_analysis(self.cfg, "pilot")
        dec = pd.read_csv(Path(self.cfg["paths"]["tables"]) / "ig_source_decision.csv", encoding="utf-8-sig").set_index("tag")
        all_tags = pd.read_csv(self.tmp / "tags.csv", encoding="utf-8-sig", dtype=str)
        self.assertEqual(len(dec), len(all_tags))                                         # CSV 의 모든 태그 행
        self.assertTrue(dec.loc["노즈워크", "판정"].startswith("제외"))
        self.assertEqual(int(dec.loc["펫캠", "최종 문서 수"]), 1)
        self.assertTrue(dec.loc["제트봇", "판정"].startswith("미확정"))

    def test_load_raw_ignores_instagram_side_files(self):
        self.run_it(["로봇청소기강아지"])
        Path(self.cfg["paths"]["raw"], "x_v1.csv").write_text("a,b\n1,2\n", "utf-8")
        docs = load_raw({**self.cfg, "corpus": {}})                       # corpus.sources 없는 옛 설정: 보조 파일만 제외하고 전부
        self.assertEqual(set(docs["source"]), {"instagram"})
        self.assertTrue(load_raw(self.cfg).empty)                        # 기본 설정: instagram 은 A 코퍼스에서 제외


if __name__ == "__main__":
    unittest.main()
