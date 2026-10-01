"""인스타(정제본) 군집 분석 → 가설 DB 매핑.

노트북(카페 크롤링)과 같은 방식: 정규식 정제 → Okt 형태소(명사·형용사·동사) → 불용어 → TF-IDF → KMeans.
차이점: 광고 의심 제거, 게시물/댓글 분리, k 는 실루엣으로 선택(4~10).

입력: outputs/tables/ig_clean_1033.csv
출력: outputs/tables/ig_cluster_*.csv, ig_cluster_hypo.xlsx
실행: python -m analysis.ig_cluster_hypo   (pet_needs_pipeline 폴더에서, Java 필요: konlpy Okt)
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd
from konlpy.tag import Okt
from sklearn.cluster import KMeans
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.metrics.pairwise import cosine_similarity

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "outputs/tables/ig_clean_1033.csv"
OUT = ROOT / "outputs/tables"
K_RANGE = range(4, 11)
TEMPLATE_DUP = 3   # 앞 15자가 같은 글이 이 개수를 넘으면 템플릿(반복 게시·이벤트 댓글)으로 보고 1건만 남김
MIN_TOKENS = 3
EXTRA_STOP = {  # 검색 태그·제품 단어: 모든 글에 나와 군집 구분에 도움 안 됨
    "펫캠", "홈캠", "카메라", "캠", "cctv", "펫", "강아지", "반려견", "댕댕이", "우리", "저희", "제", "하다", "있다",
    "되다", "이다", "것", "수", "거", "좀", "더", "진짜", "너무", "정말", "그냥", "오다", "보다", "같다", "없다", "않다",
}

# 가설ID → 해당 문맥 정규식 (문서 단위 매칭, 군집별 비중 산출용)
HYPO_RX = {
    "H2 문제행동": r"짖|하울링|낑낑|배변|파괴|뜯|분리불안",
    "H3 불안·죄책감": r"미안|죄책감|불안|걱정|안쓰|짠하|마음 ?아프",
    "H4 피곤·못놀아줌": r"피곤|퇴근|못 ?놀아|놀아주지",
    "H5 사각지대·고정형": r"사각|화면 ?밖|안 ?보|고정|회전|각도|시야",
    "H5-a 양방향·간식·음성": r"간식|양방향|음성|말 ?걸|목소리|토킹|디스펜서",
    "H5-a 반응없음·질림": r"반응 ?없|무반응|질려|금방|소용|시큰둥",
    "H6 알림·실시간대응": r"알림.{0,10}(?:못|안)|못 ?봐|못 ?봄|일하다|회의",
    "A2 알림 피로": r"알림.{0,10}(?:많|너무|피곤|자주)|알람",
    "H7 원하는 기능": r"있었으면|됐으면|되면 좋|바라|원해|필요",
    "R1 보안·프라이버시": r"해킹|보안|프라이버시|사생활|유출|훔쳐|도청",
    "S1 돌봄공백": r"혼자|외출|출근|집 ?비|비운|집에 ?없|두고",
    "S4 노즈워크·장난감": r"노즈워크|장난감|퍼즐|터그",
}
SHARE_MIN = 0.25  # 군집 문서 중 이 비율 이상이 해당 정규식에 걸리면 가설 후보로 표시


def load_stop() -> set:
    words = {l.strip() for l in (ROOT / "config/stopwords.txt").read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")}
    return words | EXTRA_STOP


def clean(text: str) -> str:
    t = re.sub(r"https?://\S+|@\w+|#\S+", " ", str(text))  # 해시태그는 군집 입력에서 제외(가설 키워드 비율은 원문 사용)
    return re.sub(r"[^a-zA-Z0-9가-힣\s]", " ", t)


def drop_templates(df: pd.DataFrame) -> pd.DataFrame:
    key = df["body"].map(lambda t: re.sub(r"[^가-힣a-zA-Z0-9]", "", clean(t))[:15])
    cnt = key.map(key.value_counts())
    keep = (cnt <= TEMPLATE_DUP) | ~key.duplicated()
    return df[keep & key.ne("")]


def tokenize(df: pd.DataFrame, okt: Okt, stop: set) -> pd.DataFrame:
    toks = []
    for t in df["body"]:
        pos = okt.pos(clean(t), stem=True, norm=True)
        toks.append([w for w, p in pos if p in ("Noun", "Adjective", "Verb") and len(w) > 1 and w.lower() not in stop])
    df = df.copy()
    df["tokens"] = toks
    df["text"] = df["tokens"].map(" ".join)
    return df[df["tokens"].map(len) >= MIN_TOKENS].reset_index(drop=True)


def cluster(df: pd.DataFrame, label: str):
    vec = TfidfVectorizer(tokenizer=str.split, token_pattern=None, lowercase=False,
                          min_df=2, sublinear_tf=True)
    X = vec.fit_transform(df["text"])
    rows = []
    for k in K_RANGE:
        km = KMeans(n_clusters=k, random_state=42, n_init=20).fit(X)
        rows.append({"세트": label, "k": k, "silhouette": round(silhouette_score(X, km.labels_, metric="cosine"), 4)})
    ks = pd.DataFrame(rows)
    best = int(ks.loc[ks["silhouette"].idxmax(), "k"])
    ks["선택"] = ks["k"].eq(best).map({True: "★", False: ""})
    km = KMeans(n_clusters=best, random_state=42, n_init=20).fit(X)
    df = df.copy()
    df["cluster"] = km.labels_
    terms = vec.get_feature_names_out()
    summ, reps = [], []
    for c in range(best):
        sub = df[df["cluster"] == c]
        top = [terms[i] for i in km.cluster_centers_[c].argsort()[::-1] if km.cluster_centers_[c][i] > 0][:10]
        row = {"세트": label, "군집": c, "문서수": len(sub), "비중(%)": round(len(sub) / len(df) * 100, 1),
               "대표 키워드": ", ".join(top)}
        hits = []
        for hid, rx in HYPO_RX.items():
            share = sub["body"].str.contains(rx, regex=True).mean()
            row[f"{hid.split()[0]} 비율"] = round(share, 2)
            if share >= SHARE_MIN:
                hits.append(f"{hid}({share:.0%})")
        row["가설 후보(≥25%)"] = " / ".join(hits)
        summ.append(row)
        pos = np.flatnonzero(df["cluster"].to_numpy() == c)
        sim = cosine_similarity(X[pos], km.cluster_centers_[c].reshape(1, -1)).ravel()
        for p in pos[sim.argsort()[::-1][:3]]:
            r = df.iloc[p]
            reps.append({"세트": label, "군집": c, "url": r["url"], "target": r.get("target", ""),
                         "emotions": r.get("emotions", ""), "본문": str(r["body"])[:400].replace("\n", " ")})
    return ks, pd.DataFrame(summ).sort_values("문서수", ascending=False), pd.DataFrame(reps)


def hypo_overall(raw: pd.DataFrame, used: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, d in [("전체(광고 포함)", raw), ("광고 제외", used)]:
        for hid, rx in HYPO_RX.items():
            n = int(d["body"].str.contains(rx, regex=True).sum())
            rows.append({"기준": name, "가설": hid, "문서수": n, "비율(%)": round(n / len(d) * 100, 1), "모수": len(d)})
    return pd.DataFrame(rows)


def main():
    raw = pd.read_csv(SRC, encoding="utf-8-sig", dtype={"promo_suspect": int})
    raw["body"] = raw["body"].fillna("")
    used = raw[raw["promo_suspect"] == 0].drop_duplicates("body").reset_index(drop=True)
    print(f"원본 {len(raw)} → 광고 제외·중복 제거 {len(used)}")
    okt, stop = Okt(), load_stop()
    funnel, ks_all, sm_all, rp_all = [], [], [], []
    for label, d in [("게시물", used[used.feed_type == "post"]), ("댓글", used[used.feed_type == "comment"])]:
        dt = drop_templates(d)
        tk = tokenize(dt, okt, stop)
        funnel.append({"세트": label, "광고 제외 후": len(d), "템플릿 중복 제거 후": len(dt), f"토큰 {MIN_TOKENS}개 이상(군집 대상)": len(tk)})
        if len(tk) < 30:
            print(label, "문서 부족 → 군집 생략", len(tk))
            continue
        ks, sm, rp = cluster(tk, label)
        ks_all.append(ks); sm_all.append(sm); rp_all.append(rp)
        print(f"[{label}] n={len(tk)} k={int(ks.loc[ks['선택'] == '★', 'k'].iloc[0])}")
    outs = {"ig_cluster_funnel": pd.DataFrame(funnel), "ig_cluster_k_select": pd.concat(ks_all),
            "ig_cluster_summary": pd.concat(sm_all), "ig_cluster_representatives": pd.concat(rp_all),
            "ig_hypo_keyword_share": hypo_overall(raw, used)}
    with pd.ExcelWriter(OUT / "ig_cluster_hypo.xlsx") as xw:
        for name, df in outs.items():
            df.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8-sig")
            df.to_excel(xw, sheet_name=name.replace("ig_", "")[:31], index=False)
    print(outs["ig_cluster_summary"][["세트", "군집", "문서수", "비중(%)", "대표 키워드", "가설 후보(≥25%)"]].to_string())


if __name__ == "__main__":
    main()
