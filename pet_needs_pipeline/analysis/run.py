"""분석 단계 묶음: 결과 표를 outputs/tables/ 에 CSV로 저장하고 dict로 돌려준다."""
import logging
from pathlib import Path

import pandas as pd

from analysis.features import feature_summary, feed_compare, rating_check, sentence_scores, tag_features
from analysis.topics import run_topics

log = logging.getLogger("pipeline")


def run_analysis(df: pd.DataFrame, funnel: pd.DataFrame, cfg: dict) -> dict:
    out = Path(cfg["paths"]["tables"])
    out.mkdir(parents=True, exist_ok=True)
    df = tag_features(df, cfg)
    sents = sentence_scores(df, cfg)
    res = {
        "docs": df,
        "funnel": funnel,
        "summary": feature_summary(df, sents, cfg),
        "feed": feed_compare(df, sents, cfg),
        "rating": rating_check(df, sents),
        "source_mix": df.groupby(["source", "query_group"]).size().unstack(fill_value=0),
        "feed_mix": df["feed_type"].value_counts().rename_axis("급여 방식").reset_index(name="문서 수"),
    }
    res["topics"], res["k_select"], res["topic_docs"] = run_topics(df, cfg)
    for name in ("funnel", "summary", "feed", "rating", "feed_mix", "topics", "k_select"):
        res[name].to_csv(out / f"{name}.csv", index=False, encoding="utf-8-sig")
    res["source_mix"].to_csv(out / "source_mix.csv", encoding="utf-8-sig")
    sents.sort_values("neg_score", ascending=False).head(300).to_csv(
        out / "top_complaint_sentences.csv", index=False, encoding="utf-8-sig")   # 원문 검토용
    s = res["summary"]
    log.info("기능 우선순위:\n%s", s[["기능", "언급률", "불만율", "기회 점수", "표본 부족"]].to_string(index=False))
    return res
