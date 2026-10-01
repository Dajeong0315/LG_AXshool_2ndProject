"""리뷰 CSV 4종(로보락·삼성/LG 로봇청소기, 샤오미·티피링크 홈캠)으로 가설 근거 차트·표 생성.

사용법:
    python analyze_reviews.py <CSV 폴더> [출력 폴더]

CSV 폴더에는 파일명에 아래 키워드가 들어간 CSV가 있어야 한다.
    로보락 청소기: 이름에 'robo' 없음 → 컬럼 brand == '로보락'
    삼성/LG      : is_event 컬럼이 있는 파일
    홈캠         : brand in {'샤오미','티피링크'}
의존성: pandas, scipy, matplotlib, koreanize-matplotlib
"""
import glob
import re
import sys
from pathlib import Path

import koreanize_matplotlib  # noqa: F401  (한글 폰트 등록)
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "outputs"
(OUT / "charts").mkdir(parents=True, exist_ok=True)
(OUT / "tables").mkdir(parents=True, exist_ok=True)

# ---------- 색 (dataviz reference palette: blue / neutral gray) ----------
BLUE, GRAY = "#2a78d6", "#a8a7a1"
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e6e5e1"

# ---------- 키워드 사전 (정규식, 팀 합의 후 조정) ----------
KW = {
    "털·머리카락": r"털|머리카락|모발",
    "외출·집 비움": r"외출|출근|집.{0,4}비울|없을 때|없는 동안|혼자",
    "움직임·추적": r"움직임|추적|따라다|회전|팬틸트|360|시야",
    "알림": r"알림|푸시",
    "배설물·오작동 우려": r"불안|걱정|무섭|겁|오작동|배설|응가|똥|사고|끼임|갇",
    "알아서·자동": r"알아서|자동으로|손.{0,3}안",
    "앱·원격": r"원격|앱|어플|씽큐|스마트싱스",
}
TOPICS = {  # 반려 청소기 리뷰 언급 주제 (복수 응답)
    "털·먼지 청소": r"털|머리카락|먼지",
    "시간 절약·편의": r"시간|편하|편리|귀찮|힘들|수고",
    "외출 중·자동 청소": r"출근|외출|알아서|자동으로",
    "배설물·사고 대응": r"배설|똥|응가|오줌|소변|사고",
    "냄새·위생": r"냄새|탈취|위생",
    "물걸레·얼룩 관리": r"물걸레|걸레|발자국|얼룩",
    "반려동물 반응": r"무서|놀라|짖|좋아하|싫어|겁|호기심|따라",
}


def load(path):
    return pd.read_csv(path, encoding="utf-8-sig")


files = sorted(glob.glob(str(SRC / "*.csv")))
frames = [load(f) for f in files]
vac_parts, cam_parts = [], []
for d in frames:
    if "is_event" in d.columns:
        vac_parts.append(d)
    elif d["brand"].iloc[0] == "로보락":
        d = d.assign(is_event=False)
        vac_parts.append(d)
    else:
        cam_parts.append(d)
vac = pd.concat(vac_parts, ignore_index=True)
cam = pd.concat(cam_parts, ignore_index=True)
for d in (vac, cam):
    d["is_pet"] = d["is_pet"].astype(str).eq("True")
vac["is_event"] = vac["is_event"].astype(str).eq("True")
vac["review"] = vac["review"].fillna("")
cam["review"] = cam["review"].fillna("")


def flag(df, pat):
    return df["review"].str.contains(pat, regex=True)


def wilson(k, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return c - h, c + h


def compare(df, key, pat, group_cols=()):
    """is_pet True/False 별 언급률 + 카이제곱(Fisher 대체 없음) + 오즈비."""
    rows = []
    groups = [((), df)] if not group_cols else list(df.groupby(list(group_cols)))
    for gk, g in groups:
        gk = gk if isinstance(gk, tuple) else (gk,)
        f = flag(g, pat)
        a = int((f & g.is_pet).sum()); n1 = int(g.is_pet.sum())
        b = int((f & ~g.is_pet).sum()); n0 = int((~g.is_pet).sum())
        tbl = [[a, n1 - a], [b, n0 - b]]
        try:
            chi2, p, _, _ = stats.chi2_contingency(tbl)
        except ValueError:
            p = np.nan
        orr = (a * (n0 - b)) / max((n1 - a) * b, 1e-9) if b else np.nan
        lo1, hi1 = wilson(a, n1); lo0, hi0 = wilson(b, n0)
        rows.append(dict(group="/".join(map(str, gk)) or "전체", keyword=key,
                         pet_k=a, pet_n=n1, pet_rate=a / n1 if n1 else np.nan, pet_lo=lo1, pet_hi=hi1,
                         non_k=b, non_n=n0, non_rate=b / n0 if n0 else np.nan, non_lo=lo0, non_hi=hi0,
                         odds_ratio=orr, p_value=p))
    return pd.DataFrame(rows)


def style(ax, xlabel=None):
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0)
    ax.xaxis.grid(True, color=GRID, lw=0.8); ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK2)


def pstr(p):
    return "p<0.001" if p < 0.001 else f"p={p:.3f}"


def save(fig, name):
    fig.savefig(OUT / "charts" / name, dpi=200, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def grouped_bars(res, title, fname, note):
    """res: compare() 결과(group별). 반려=파랑, 비반려=회색. 값 라벨 + n."""
    res = res.reset_index(drop=True)
    y = np.arange(len(res))
    h = 0.34
    fig, ax = plt.subplots(figsize=(8.6, 1.5 + 1.15 * len(res)))
    ax.barh(y - h / 2, res.pet_rate * 100, h - 0.04, color=BLUE, label="반려 리뷰")
    ax.barh(y + h / 2, res.non_rate * 100, h - 0.04, color=GRAY, label="비반려 리뷰")
    for i, r in res.iterrows():
        ax.text(r.pet_rate * 100 + 0.8, i - h / 2, f"{r.pet_rate*100:.1f}%  (n={r.pet_n:,})", va="center", color=INK, fontsize=9.5)
        ax.text(r.non_rate * 100 + 0.8, i + h / 2, f"{r.non_rate*100:.1f}%  (n={r.non_n:,})", va="center", color=INK2, fontsize=9.5)
        ax.text(1.0, i, f"{pstr(r.p_value)}", transform=ax.get_yaxis_transform(), ha="right", va="center", color=INK2, fontsize=9)
    ax.set_yticks(y); ax.set_yticklabels(res.group, color=INK, fontsize=10.5)
    ax.invert_yaxis(); style(ax, "해당 키워드를 언급한 리뷰 비율 (%)")
    ax.set_xlim(0, max(res.pet_rate.max(), res.non_rate.max()) * 100 * 1.45)
    ax.legend(loc="upper right", bbox_to_anchor=(1.0, 1.1), ncol=2, frameon=False, labelcolor=INK2)
    ax.set_title(title, loc="left", color=INK, fontsize=13, fontweight="bold", pad=14)
    fig.text(0.01, -0.02, note, color=INK2, fontsize=8.5, ha="left", va="top", wrap=True)
    save(fig, fname)


tables = {}

# ================= 1. 털 언급률 (청소기, 브랜드별) =================
vac_main = vac[~vac.is_event]          # 이벤트 리뷰 제외(기본)
r1 = compare(vac_main, "털·머리카락", KW["털·머리카락"], ["brand"])
r1_all = compare(vac, "털·머리카락", KW["털·머리카락"], ["brand"]).assign(scope="이벤트 포함")
tables["01_털언급률_청소기"] = pd.concat([r1.assign(scope="이벤트 제외(기본)"), r1_all])
order = {"로보락": 0, "LG": 1, "삼성": 2}
r1 = r1.sort_values("group", key=lambda s: s.map(order))
grouped_bars(r1, "반려 리뷰는 '털'을 4.5~9배 더 자주 말한다 (로봇청소기)", "01_털언급률_청소기.png",
             "자료: 로보락·LG·삼성 로봇청소기 리뷰(이벤트 리뷰 제외). 반려=리뷰 본문에 반려동물 키워드 포함. 키워드: 털·머리카락·모발. p값: 카이제곱 검정.")

# ================= 2. 반려 청소기 리뷰 언급 주제 =================
pet_vac = vac_main[vac_main.is_pet]
t2 = pd.DataFrame([dict(topic=k, k=int(flag(pet_vac, p).sum()), n=len(pet_vac)) for k, p in TOPICS.items()])
t2["rate"] = t2.k / t2.n
tables["02_반려청소기_언급주제"] = t2.sort_values("rate", ascending=False)
t2s = t2.sort_values("rate", ascending=True)
fig, ax = plt.subplots(figsize=(8.4, 4.6))
cols = [BLUE if t == "털·먼지 청소" else GRAY for t in t2s.topic]
ax.barh(t2s.topic, t2s.rate * 100, color=cols, height=0.62)
for i, (r, k) in enumerate(zip(t2s.rate, t2s.k)):
    ax.text(r * 100 + 1, i, f"{r*100:.0f}%  ({k}건)", va="center", color=INK, fontsize=10)
style(ax, "반려 리뷰 중 해당 주제를 언급한 비율 (%, 복수 응답)")
ax.set_xlim(0, t2.rate.max() * 100 * 1.25)
ax.tick_params(axis="y", labelsize=10.5, labelcolor=INK)
top = t2s.iloc[-1].topic
ax.set_title(f"반려 리뷰의 주요 언급 주제 (1위: {top}, 반려 리뷰 {len(pet_vac)}건)", loc="left", color=INK, fontsize=13, fontweight="bold", pad=14)
fig.text(0.01, -0.02, "자료: 로보락·LG·삼성 로봇청소기 반려 리뷰(이벤트 제외). 주제별 키워드 사전 기반 언급 여부(복수 응답).", color=INK2, fontsize=8.5)
save(fig, "02_반려청소기_언급주제.png")

# ================= 3. 홈캠 반려 vs 비반려 용도 =================
rows3 = pd.concat([compare(cam, k, KW[k]) .assign(group=k) for k in ["외출·집 비움", "움직임·추적", "알림"]])
tables["03_홈캠_반려용도"] = rows3
grouped_bars(rows3.reset_index(drop=True), "펫캠 사용자는 '집을 비울 때'와 '움직임 추적'을 더 말한다 (홈캠)", "03_홈캠_반려용도.png",
             "자료: 샤오미 홈캠 3세대·티피링크 Tapo C210 리뷰(각 1종). 반려=반려동물 키워드 포함. '알림'은 언급 자체가 적어 해석에 주의. p값: 카이제곱 검정.")

# ================= 4. 자율 실행: 자동 선호 vs 우려 (청소기) =================
rows4 = pd.concat([compare(vac_main, k, KW[k]).assign(group=k) for k in ["알아서·자동", "배설물·오작동 우려", "앱·원격"]])
tables["04_자율실행_자동선호vs우려"] = rows4
grouped_bars(rows4.reset_index(drop=True), "반려 리뷰는 '알아서 해주길' 원하면서 '사고 우려'도 더 많이 말한다", "04_자율실행_자동선호vs우려.png",
             "자료: 로봇청소기 리뷰(이벤트 제외). 승인형 vs 자동형 선호가 갈리는지는 리뷰로는 알 수 없음. 키워드 사전 기반 예비치.")

# ================= 5. 반려 리뷰 상위 키워드 (청소기 vs 홈캠) =================
STOP = set("마리 동안 어디 보기 필수 제자리 위치 목소리 제품 사용 구매 정말 너무 아주 같다 있다 좋다 되다 하다 그냥 진짜 생각 기능 이용 때문 주문 배송 만족 추천 가격 처음 하나 다시 계속 조금 많이 정도 사다 쓰다 보다 같이 계속 잘".split())


def top_terms(df, n=15):
    docs = df["tokens"].fillna("").astype(str).str.split()
    df_cnt = {}
    for toks in docs:
        for t in set(toks):
            df_cnt[t] = df_cnt.get(t, 0) + 1
    N = len(docs)
    out = {}
    for t, c in df_cnt.items():
        if len(t) < 2 or t in STOP or c < 5:
            continue
        out[t] = c
    return pd.Series(out).sort_values(ascending=False).head(n)


PETDEF = re.compile(r"강아지|고양이|반려|펫|댕댕|집사|애완|냥|멍멍|견$|키우|동물|아가|고영")


def lift_terms(df, n=15):
    pet, non = df[df.is_pet], df[~df.is_pet]
    def dfc(d):
        c = {}
        for toks in d["tokens"].fillna("").astype(str).str.split():
            for t in set(toks):
                c[t] = c.get(t, 0) + 1
        return c
    cp, cn = dfc(pet), dfc(non)
    rows = []
    for t, c in cp.items():
        if len(t) < 2 or t in STOP or c < 8 or PETDEF.search(t):
            continue
        rp = c / len(pet); rn = (cn.get(t, 0) + 0.5) / (len(non) + 1)
        rows.append((t, c, rp, rn, rp / rn))
    return pd.DataFrame(rows, columns=["term", "pet_docs", "pet_rate", "non_rate", "lift"]).query("lift >= 5").sort_values("pet_docs", ascending=False).head(n)


for nm, df in [("청소기", vac_main), ("홈캠", cam)]:
    lt = lift_terms(df)
    tables[f"05_반려특징어_{nm}"] = lt
    lt = lt.sort_values("pet_docs")
    fig, ax = plt.subplots(figsize=(7.2, 4.8))
    ax.barh(lt.term, lt.pet_rate * 100, color=BLUE, height=0.62)
    for i, (r, c) in enumerate(zip(lt.pet_rate, lt.pet_docs)):
        ax.text(r * 100 + 0.3, i, f"{r*100:.0f}%  ({c}건)", va="center", fontsize=9.5, color=INK)
    style(ax, "반려 리뷰 중 해당 단어를 언급한 비율 (%)")
    ax.set_xlim(0, lt.pet_rate.max() * 100 * 1.3)
    ax.set_title(f"{nm}: 반려 리뷰에 두드러지는 단어", loc="left", color=INK, fontsize=13, fontweight="bold", pad=14)
    fig.text(0.01, -0.02, "자료: tokens 컬럼(형태소 분석 결과). 반려 리뷰 8건 이상 등장, 비반려 대비 5배 이상 높은 단어만. 반려 판별용 키워드(강아지·고양이·반려 등)는 제외.", color=INK2, fontsize=8.5)
    save(fig, f"05_반려특징어_{nm}.png")

# ================= 표 저장 + 요약 =================
for k, v in tables.items():
    v.to_csv(OUT / "tables" / f"{k}.csv", index=False, encoding="utf-8-sig")

summ = pd.DataFrame([
    dict(항목="청소기 리뷰 수", 값=len(vac)), dict(항목="이벤트 리뷰", 값=int(vac.is_event.sum())),
    dict(항목="청소기 반려(이벤트 제외)", 값=int(vac_main.is_pet.sum())), dict(항목="청소기 비반려(이벤트 제외)", 값=int((~vac_main.is_pet).sum())),
    dict(항목="홈캠 리뷰 수", 값=len(cam)), dict(항목="홈캠 반려", 값=int(cam.is_pet.sum())),
])
summ.to_csv(OUT / "tables" / "00_데이터요약.csv", index=False, encoding="utf-8-sig")
print(summ.to_string(index=False))
print(tables["01_털언급률_청소기"][["scope", "group", "pet_rate", "non_rate", "odds_ratio", "p_value"]].round(4).to_string(index=False))
print(rows3[["group", "pet_k", "pet_n", "pet_rate", "non_k", "non_n", "non_rate", "p_value"]].round(4).to_string(index=False))
print(rows4[["group", "pet_rate", "non_rate", "p_value"]].round(4).to_string(index=False))
print(tables["02_반려청소기_언급주제"].round(3).to_string(index=False))
