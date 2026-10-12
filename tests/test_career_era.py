# Research source excerpt. Not the original operational deployment.
# See docs/CODE_CHANGELOG.md and src/README.md for omissions and dependencies.
# -*- coding: utf-8 -*-
"""TDD: career_stats_from_splits — 합산 후 rate 재계산(rate 평균 아님) + as-of 형식."""
from _mlb_career_era import career_stats_from_splits, ip_conv


def _sp(er, ip, h=0, bb=0, k=0, hr=0):
    return {'stat': {'earnedRuns': er, 'inningsPitched': ip, 'hits': h,
                     'baseOnBalls': bb, 'strikeOuts': k, 'homeRuns': hr}}


def run():
    cases = []

    # 1) rate 평균이 아니라 합산-후-rate: s1 ERA3.60(40/100), s2 ERA7.20(40/50)
    #    rate평균=5.40(틀림) / 정답 career=80/150*9=4.80
    r = career_stats_from_splits([_sp(40, 100), _sp(40, 50)])
    cases.append(("합산후rate (avg 5.40 아닌 4.80)", abs(r['era'] - 4.80) < 0.01))

    # 2) IP 변환 1.1 = 1⅓
    cases.append(("ip_conv(1.1)=1.333", abs(ip_conv(1.1) - 1.3333) < 0.01))
    #    9.2이닝 = 9⅔ = 9.667 합산 확인
    r2 = career_stats_from_splits([_sp(0, 9.2)])  # IP=9.667
    cases.append(("inningsPitched 9.2→9.667", abs(r2['ip'] - 9.7) < 0.05))

    # 3) WHIP = (H+BB)/IP, K9 = 9*K/IP
    r3 = career_stats_from_splits([_sp(30, 90, h=80, bb=28, k=99, hr=10)])
    cases.append(("whip=(80+28)/90=1.20", abs(r3['whip'] - 1.20) < 0.01))
    cases.append(("k9=9*99/90=9.90", abs(r3['k9'] - 9.90) < 0.01))
    cases.append(("era=9*30/90=3.00", abs(r3['era'] - 3.00) < 0.01))

    # 4) 방어: 빈 splits / IP=0 → {}
    cases.append(("빈 splits → {}", career_stats_from_splits([]) == {}))
    cases.append(("IP=0 → {}", career_stats_from_splits([_sp(5, 0)]) == {}))
    cases.append(("None 안전", career_stats_from_splits(None) == {}))

    # 5) 공식 일치 가드 (_mlb_asof_prior_true: 9*ER/IP)
    er, ip = 77, 200.0
    cases.append(("9*ER/IP 공식 일치", abs(career_stats_from_splits([_sp(er, ip)])['era'] - round(9 * er / ip, 2)) < 1e-9))

    allp = True
    for nm, ok in cases:
        allp &= ok
        print(f"  {'PASS' if ok else 'FAIL 🔴'}  {nm}")
    print("\n  => " + ("ALL GREEN" if allp else "RED"))
    return allp


if __name__ == '__main__':
    run()
