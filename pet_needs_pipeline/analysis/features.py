"""기능별 언급률 · 불만 강도 · 기회 점수 · 급여 방식별 비교 (성공 기준 1)."""
import logging

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency

from preprocess.clean import read_list

log = logging.getLogger("pipeline")


def load_weighted(path: str) -> dict[str, float]:
    out = {}
    for line in read_list(path):
        w, _, v = line.partition(",")
        out[w.strip()] = float(v) if v.strip() else 1.0
    return out


def tag_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    for f, terms in cfg["feature_dict"].items():
        df[f"f_{f}"] = df["text"].map(lambda t: any(w in t for w in terms))
    df["f_any"] = df[[f"f_{f}" for f in cfg["feature_dict"]]].any(axis=1)
    return df


def sentence_scores(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """기능 단어가 들어간 문장마다 부정 점수(가중합)와 불만 여부."""
    neg = load_weighted(cfg["paths"]["negative_words"])
    pos = read_list(cfg["paths"]["positive_words"])
    rows = []
    for i, r in df.iterrows():
        for s in str(r["sentences"]).split(" || "):
            neg_score = sum(v for w, v in neg.items() if w in s)
            pos_cnt = sum(1 for w in pos if w in s)
            for f, terms in cfg["feature_dict"].items():
                if any(w in s for w in terms):
                    rows.append({"doc": i, "feature": f, "neg_score": neg_score,
                                 "complaint": neg_score > 0 and neg_score > pos_cnt, "sentence": s[:200]})
    return pd.DataFrame(rows, columns=["doc", "feature", "neg_score", "complaint", "sentence"])


def feature_summary(df: pd.DataFrame, sents: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    explore = df[df["query_group"] == "explore"]
    base, basis = (explore, "탐색 수집분") if len(explore) >= 30 else (df, "전체(탐색분 30건 미만)")
    out = []
    for f, label in cfg["feature_labels"].items():
        s = sents[sents["feature"] == f]
        out.append({
            "feature": f, "기능": label,
            "언급 문서 수(전체)": int(df[f"f_{f}"].sum()),
            "언급률": base[f"f_{f}"].mean() if len(base) else np.nan,
            "언급률 기준": f"{basis} n={len(base)}",
            "기능 문장 수": len(s),
            "불만율": s["complaint"].mean() if len(s) else np.nan,
            "불만 강도(평균 부정점수)": s["neg_score"].mean() if len(s) else np.nan,
        })
    t = pd.DataFrame(out)
    t["기회 점수"] = t["언급률"] * t["불만율"]
    t["표본 부족"] = t["언급 문서 수(전체)"] < cfg["min_feature_docs"]
    t["순위"] = t["기회 점수"].rank(ascending=False, method="min")
    return t.sort_values("순위")


def feed_compare(df: pd.DataFrame, sents: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """생식·화식 vs 건사료: 기능별 언급률·불만율 + 언급률 차이 카이제곱 p값."""
    groups = ["생식·화식", "건사료"]
    sub = df[df["feed_group"].isin(groups)]
    s = sents.merge(df[["feed_group"]], left_on="doc", right_index=True)
    rows = []
    for f, label in cfg["feature_labels"].items():
        row = {"feature": f, "기능": label}
        table = []
        for g in groups:
            d = sub[sub["feed_group"] == g]
            row[f"{g} 문서 수"] = len(d)
            row[f"{g} 언급률"] = d[f"f_{f}"].mean() if len(d) else np.nan
            ss = s[(s["feature"] == f) & (s["feed_group"] == g)]
            row[f"{g} 불만율"] = ss["complaint"].mean() if len(ss) else np.nan
            table.append([int(d[f"f_{f}"].sum()), int((~d[f"f_{f}"]).sum())])
        try:
            row["언급률 차이 p값"] = chi2_contingency(table)[1]
        except ValueError:                                    # 빈 칸이 있으면 검정 불가
            row["언급률 차이 p값"] = np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def rating_check(df: pd.DataFrame, sents: pd.DataFrame) -> pd.DataFrame:
    """별점 있는 리뷰로 부정사전 검증: 별점 1~2 = 불만 정답."""
    r = df[df["rating"].notna()] if "rating" in df else df.iloc[0:0]
    if r.empty:
        return pd.DataFrame([{"리뷰 수": 0, "비고": "별점 데이터 없음"}])
    flag = sents.groupby("doc")["complaint"].any().reindex(r.index, fill_value=False)
    truth = r["rating"] <= 2
    tp, fp, fn = int((flag & truth).sum()), int((flag & ~truth).sum()), int((~flag & truth).sum())
    prec = tp / (tp + fp) if tp + fp else np.nan
    rec = tp / (tp + fn) if tp + fn else np.nan
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else np.nan
    return pd.DataFrame([{"리뷰 수": len(r), "별점≤2": int(truth.sum()), "정밀도": prec, "재현율": rec, "F1": f1}])
