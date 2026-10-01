"""LDA 토픽 모델링 — 기능 사전에 안 걸린 잔여 문서에서 신규 페인포인트 후보 찾기 (성공 기준 2)."""
import logging

import numpy as np
import pandas as pd
from sklearn.decomposition import LatentDirichletAllocation
from sklearn.feature_extraction.text import CountVectorizer

from analysis.features import load_weighted

log = logging.getLogger("pipeline")


def umass(X_bin, top_idx) -> float:
    """UMass coherence (높을수록 좋음, 보통 음수)."""
    df = np.asarray(X_bin.sum(axis=0)).ravel()
    score, pairs = 0.0, 0
    for i in range(1, len(top_idx)):
        for j in range(i):
            wi, wj = top_idx[i], top_idx[j]
            co = X_bin[:, wi].multiply(X_bin[:, wj]).sum()
            score += np.log((co + 1) / max(df[wj], 1))
            pairs += 1
    return score / max(pairs, 1)


def vectorize(texts, lc: dict):
    min_df = lc["min_df"]
    while True:
        try:
            vec = CountVectorizer(tokenizer=str.split, token_pattern=None, lowercase=False,
                                  min_df=min_df, max_df=lc["max_df"] if len(texts) > 20 else 1.0,
                                  max_features=lc["max_features"])
            return vec, vec.fit_transform(texts)
        except ValueError:                                   # 문서가 적어 단어가 0개면 min_df를 낮춘다
            if min_df <= 1:
                raise
            min_df = 1


def run_topics(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    lc = cfg["lda"]
    base = df[~df["f_any"]] if lc.get("residual_only", True) else df
    note = "잔여 문서(기능 사전 미언급)"
    if len(base) < max(20, lc["n_topics"] * 5):
        base, note = df, "전체 문서(잔여 문서 부족)"
    if lc.get("exclude_query_terms", True):                  # 검색어·급여방식 단어는 토픽을 독식하므로 뺀다
        from preprocess.clean import Tokenizer
        tok = Tokenizer(cfg)
        words = [w for g in cfg["keywords"].values() for w in g]
        words += [w for s in cfg["sites"].values() for g in (s.get("keywords") or {}).values() for w in g]
        words += [w for g in cfg["feed_type_dict"].values() for w in g]
        drop = {t for w in words for t in tok.tokens(w)} | {w.lower() for w in words}
        base = base.assign(tokens=base["tokens"].fillna("").map(lambda t: " ".join(x for x in t.split() if x not in drop)))
    base = base[base["tokens"].fillna("").str.len() > 0]
    if len(base) < 5:
        log.warning("토픽 모델링 건너뜀: 문서 %d건", len(base))
        empty = pd.DataFrame(columns=["토픽", "상위 키워드", "문서 비중", "불만 문서 비율", "대표 문장"])
        return empty, pd.DataFrame(), base

    vec, X = vectorize(base["tokens"].tolist(), lc)
    vocab = np.array(vec.get_feature_names_out())
    X_bin = (X > 0).astype(int).tocsc()
    top_n = min(lc["top_n_words"], len(vocab))

    k_rows = []
    k = min(lc["n_topics"], max(2, len(base) // 5))
    if lc.get("auto_k"):
        for kc in [c for c in lc["k_candidates"] if c < len(base)]:
            m = LatentDirichletAllocation(n_components=kc, random_state=42, learning_method="batch").fit(X)
            coh = np.mean([umass(X_bin, t.argsort()[::-1][:top_n]) for t in m.components_])
            k_rows.append({"k": kc, "UMass coherence": coh, "perplexity": m.perplexity(X)})
        if k_rows:
            k = int(max(k_rows, key=lambda r: r["UMass coherence"])["k"])

    lda = LatentDirichletAllocation(n_components=k, random_state=42, learning_method="batch").fit(X)
    doc_topic = lda.transform(X)
    base = base.assign(topic=doc_topic.argmax(axis=1), topic_p=doc_topic.max(axis=1))
    neg = load_weighted(cfg["paths"]["negative_words"])
    base["has_neg"] = base["text"].map(lambda t: any(w in t for w in neg))

    rows = []
    for t in range(k):
        top = vocab[lda.components_[t].argsort()[::-1][:top_n]]
        d = base[base["topic"] == t]
        rep = d.sort_values("topic_p", ascending=False)["text"].head(1)
        rows.append({"토픽": f"T{t + 1}", "상위 키워드": ", ".join(top),
                     "문서 비중": len(d) / len(base), "불만 문서 비율": d["has_neg"].mean() if len(d) else np.nan,
                     "대표 문장": rep.iloc[0][:80] if len(rep) else ""})
    topics = pd.DataFrame(rows).sort_values("문서 비중", ascending=False)
    topics["대상"] = f"{note} n={len(base)}, k={k}"
    return topics, pd.DataFrame(k_rows), base
