"""대체재 감정 데이터 정제 테스트 (고양이 필터 · 홍보 필터 · target 분류 · 검토 샘플). 오프라인, Apify 호출 없음."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from analysis.ig_emotion_clean import (clean_docs, emotion_by_target, load_markers, review_sample,   # noqa: E402
                                       run_emotion_clean)
from crawlers.instagram_apify import read_words                                                      # noqa: E402

CFG = yaml.safe_load(Path("config/config.yaml").read_text("utf-8"))
EMO = read_words("config/ig_emotion_words.txt")
MK = load_markers("config/ig_promo_markers.txt")


def run(*bodies):
    df = pd.DataFrame({"url_hash": [f"h{i}" for i in range(len(bodies))], "body": list(bodies), "keyword": "펫캠", "feed_type": "comment"})
    return clean_docs(df, CFG, EMO, MK).set_index("url_hash")


class TestEmotionClean(unittest.TestCase):
    def test_cat_filter_keeps_dog_docs(self):
        d = run("고양이 혼자 두고 출근해서 걱정돼요", "집사님 부럽다옹 안심이네요", "강아지랑 고양이 둘이 있어서 걱정돼요", "냥이 때문에 불안해요 우리 푸들은 괜찮아요")
        self.assertEqual(list(d["status"]), ["removed_cat", "removed_cat", "kept", "kept"])

    def test_promo_removed_but_security_worry_kept(self):
        d = run("라이브뷰, 세이프케어 👏 언제나 든든하고 안심이되네요",                                   # 홍보 → 제거
                "이 정도 보안이면 완전 안심이지🔥🔥❤️",                                                  # 👏🔥 반복 → 제거
                "요즘 홈카메라 해킹 뉴스 볼 때마다 불안해서 설치 망설였는데 암호화된다니 안심이네요",          # 보안 우려 + 약한 표지 → 유지 + 플래그
                "해킹 걱정돼서 라이브뷰 쓰는 게 망설여져요",                                              # 강한 표지 + 보안 우려 → 유지 + 플래그
                "우리 강아지 혼자 있을 때 걱정돼서 샀어요",                                                # 일반 글
                "스펙은 그냥 그런데 화질은 안심될 정도예요")                                               # 약한 표지만 → 유지 + 플래그
        self.assertEqual(list(d["status"]), ["removed_promo", "removed_promo", "kept", "kept", "kept", "kept"])
        self.assertEqual(list(d["promo_suspect"]), [0, 0, 1, 1, 0, 1])

    def test_protect_brand_and_discount_markers(self):
        d = run("#내돈내산 홈캠 고민하다가 이벤트 참여했어요 만족합니다",      # 강한 표지(이벤트) + 보호 표지(내돈내산) → 유지 + 플래그
                "직접 사서 써봤는데 쿠폰 받아서 싸게 샀어요 만족",          # 보호 표지 '직접 사서'
                "실사용 후기 링크 클릭해서 샀어요 만족해요",               # 보호 표지 '실사용'
                "할인 행사하길래 샀어요 화질 좋아요",                     # 할인은 이제 약한 표지 → 유지 + 플래그
                "파인뷰 홈캠 화질 좋아요 만족합니다",                     # 브랜드명은 약한 표지 → 유지 + 플래그
                "Tapo 써보니 괜찮아요 만족합니다",                        # 대소문자 무시 브랜드
                "구매링크는 프로필 링크에 있어요 이벤트 중")                # 보호 표지 없음 → 제거
        self.assertEqual(list(d["status"]), ["kept"] * 6 + ["removed_promo"])
        self.assertEqual(list(d["promo_suspect"]), [1, 1, 1, 1, 1, 1, 0])

    def test_target_labels(self):
        d = run("우리 강아지 혼자 있을 때 걱정돼요",          # dog (사전)
                "집에 혼자 있을 때 불안해 보여서 안심이 안 돼요",   # dog (혼자 맥락)
                "해킹 걱정이 돼서 설치를 망설여요",              # security
                "너무 귀여워요",                               # other
                "누나 보고싶다구",                              # 보고싶 단독은 맥락으로 안 침 → other
                "출근하면 보고싶고 걱정돼요",                    # 감정 2개 + 보고싶 맥락 → dog
                "그냥 평범한 문장입니다")                        # 감정 없음 → target 빈 값
        self.assertEqual(list(d["target"]), ["dog", "dog", "security", "other", "other", "dog", ""])

    def test_dog_emoji_and_security_mention_flag(self):
        d = run("더 가까이 더 안심되게🐶💛", "강아지 집 cctv 해킹이 걱정돼요")
        self.assertEqual(list(d["target"]), ["dog", "dog"])                                # 강아지 증거가 있으면 dog
        self.assertEqual(list(d["security_mention"]), [0, 1])                              # 보안 언급은 따로 표시

    def test_by_target_and_review_sample_cap(self):
        bodies = [f"우리 강아지 혼자 있을 때 {w}돼요 {i}번째 글" for i in range(60) for w in ("걱정", "안심")] + ["해킹 걱정돼요 정말로"]
        d = run(*bodies)
        bt = emotion_by_target(d, EMO)
        row = bt[(bt.target == "dog") & (bt.emotion_word == "걱정")].iloc[0]
        self.assertEqual((row.doc_count, row.target_docs), (60, 120))
        self.assertEqual(int(bt[(bt.target == "security") & (bt.emotion_word == "걱정")].iloc[0].doc_count), 1)
        rv = review_sample(d.reset_index(), CFG)
        self.assertEqual(len(rv), 100)                                                     # 최대 100건
        self.assertEqual(list(rv.columns), ["url_hash", "문장", "감정", "규칙 target", "promo_suspect", "human_target", "human_is_promo"])
        self.assertTrue((rv["human_target"] == "").all() and (rv["human_is_promo"] == "").all())

    def test_end_to_end_does_not_touch_pilot_outputs(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = {**CFG, "paths": {**CFG["paths"], "raw": str(tmp / "raw"), "tables": str(tmp / "tables")}}
        (tmp / "raw").mkdir(), (tmp / "tables").mkdir()
        pd.DataFrame([dict(tag="펫캠")]).to_csv(tmp / "tables" / "ig_tag_stats_full.csv", index=False, encoding="utf-8-sig")
        pd.DataFrame([dict(source="instagram", url="u1", url_hash="a", title="", body="고양이 집사라 걱정돼요", date="2026-09-01", keyword="펫캠", feed_type="post"),
                      dict(source="instagram", url="u2", url_hash="b", title="", body="우리 강아지 혼자 있을 때 걱정돼요", date="2026-09-01", keyword="펫캠", feed_type="post"),
                      dict(source="instagram", url="u3", url_hash="c", title="", body="다른 태그 글 강아지 걱정", date="2026-09-01", keyword="제트봇", feed_type="post")]
                     ).to_csv(tmp / "raw" / "instagram.csv", index=False, encoding="utf-8-sig")
        pilot = tmp / "tables" / "ig_substitute_emotion.csv"
        pilot.write_text("emotion_word,doc_count,ratio\n걱정,1,0.5\n", "utf-8")
        before = pilot.read_bytes()
        res = run_emotion_clean(cfg)
        self.assertEqual(pilot.read_bytes(), before)
        f = res["funnel"].set_index("단계")["문서 수"]
        self.assertEqual((f["정제 대상 문서(본수집 태그의 게시물+댓글)"], f["고양이 필터 제거"], f["감정 키워드가 있는 문서"]), (2, -1, 1))
        for n in ("ig_emotion_clean_funnel", "ig_emotion_by_target", "ig_emotion_review_sample", "ig_emotion_docs_clean"):
            self.assertTrue((tmp / "tables" / f"{n}.csv").exists(), n)


if __name__ == "__main__":
    unittest.main()
