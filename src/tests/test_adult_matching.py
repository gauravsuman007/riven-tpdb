"""Tests for adult release matching.

Every case here is drawn from real Prowlarr output captured against this
instance, including the failures that motivated the module.
"""

import sys
from datetime import datetime

from program.services.scrapers.adult_matching import (
    evaluate, extract_dates, extract_volume, normalise, tokenise,
)

PASS = FAIL = 0


def check(name, condition, extra=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def ev(raw, title, site=None, performers=None, aired=None, adult=True):
    return evaluate(raw, item_title=title, site_name=site,
                    performers=performers or [], aired_at=aired,
                    is_adult_release=adult)


print("helpers")
check("normalise collapses punctuation", normalise("Pure Taboo!") == "puretaboo")
check("tokenise keeps digits", "8" in tokenise("Daddy Issues 8"))
check("tokenise drops release noise", "1080p" not in tokenise("Alpha Male 1080p WEB-DL"))
check("date yy.mm.dd", (2022, 2, 18) in extract_dates("FamilySinners.22.02.18.Ana"))
check("date yy mm dd", (2019, 6, 11) in extract_dates("PureTaboo 19 06 11 Whitney"))
check("date yyyy.mm.dd", (2021, 10, 26) in extract_dates("Site 2021.10.26 Thing"))
check("no false date from resolution", not extract_dates("Movie 1080p x264"))
check("volume from Vol.", extract_volume("Bratty Sis Vol. 10") == 10)
check("volume from trailing number", extract_volume("Daddy Issues 8") == 8)
check("year is not a volume", extract_volume("Alpha Male 2020") is None)

print("\nthe JAV flood -- the failure this module exists for")
jav = [
    "I Love You, Daddy! The Naughty Everyday Life Of Daddy And Tsubomi",
    "Yoshine Yuria - A Group Of Sugar Daddy Men Make This Damn Cheeky Brat",
    "[Caribbeancom.com] Mirai Minano - I found a sugar daddy girl",
    "Nagahama Mitsuri - Sugar Daddy Starts 1 & 2 ~College Girl Edition~",
]
for raw in jav:
    e = ev(raw, "Daddy Issues 8", site="Diabolic Video",
           performers=["Gizelle Blanco", "Kylie Rocket"],
           aired=datetime(2021, 5, 14))
    check(f"rejects {raw[:34]!r}", not e.accepted, f"reasons={e.reasons}")

print("\nmainstream collisions")
check("rejects Fight Club",
      not ev("Fight.Club.1999.1080p.BluRay.AVC.DTS-HD.MA.5.1-FGT", "Daddy Issues 8",
             site="Diabolic Video", adult=False).accepted)
check("rejects an episodic TV match",
      not ev("Shrinking S03E09 Daddy Issues 1080p ATVP WEB-DL", "Daddy Issues 8",
             site="Diabolic Video").accepted)
check("rejects Resident Alien 4x07",
      not ev("Resident Alien S04E07 Daddy Issues 1080p BluRay x264-OFT",
             "Daddy Issues 8", site="Diabolic Video").accepted)

print("\ngenuine matches")
e = ev("PureTaboo 19 06 11 Whitney Wright Alpha Male XXX 2160p MP4-KTR",
       "Alpha Male", site="Pure Taboo", performers=["Whitney Wright"],
       aired=datetime(2019, 6, 11))
check("site+date+performer accepted", e.accepted and e.site and e.date and e.performers)

check("site + title accepted",
      ev("Alpha Male [Pure Taboo 2020] WEB-DL", "Alpha Male", site="Pure Taboo").accepted)
check("site + title, spaced site name",
      ev("Deny It All You Want (Bree Mills, Pure Taboo) 2022 WEB-DL 720p",
         "Deny It All You Want", site="Pure Taboo").accepted)
check("performer + title, no site",
      ev("Paige Owens - Family's Dirty Secrets Scene 3 (2021) SiteRip",
         "Family's Dirty Secrets", site="Sweet Sinner",
         performers=["Paige Owens"]).accepted)
check("two performers corroborate",
      ev("Vanna Bardot, Steve Holmes - Deny It All You Want", "Deny It All You Want",
         site="Pure Taboo", performers=["Vanna Bardot", "Steve Holmes"]).accepted)

print("\nvolume discipline")
check("rejects a different volume",
      not ev("ZZ Unscripted Volume 4 (Brazzers) 2026 WEB-DL 720p", "Zz Unscripted 7",
             site="Brazzers").accepted)
check("accepts the right volume",
      ev("ZZ Unscripted Volume 7 (Brazzers) 2026 WEB-DL 720p", "Zz Unscripted 7",
         site="Brazzers").accepted)
check("no volume stated on release is not a conflict",
      ev("ZZ Unscripted (Brazzers) 2026 WEB-DL", "Zz Unscripted 7", site="Brazzers").accepted)
check("wrong volume rejected even with full evidence",
      not ev("Brazzers 21 05 14 Gizelle Blanco Daddy Issues Vol. 3 XXX",
             "Daddy Issues 8", site="Brazzers", performers=["Gizelle Blanco"],
             aired=datetime(2021, 5, 14)).accepted)

print("\nweak evidence must not pass")
check("title alone, not adult-flagged, rejected",
      not ev("Daddy Issues", "Daddy Issues 8", site="Diabolic Video", adult=False).accepted)
check("single common word rejected",
      not ev("Some Unrelated Family Movie", "Family Cheaters",
             site="Family Sinners").accepted)
check("empty release title is safe", not ev("", "Alpha Male", site="Pure Taboo").accepted)
check("missing metadata does not raise",
      ev("Anything At All", None, site=None, performers=None, aired=None) is not None)

print("\nordering signal")
strong = ev("PureTaboo 20 10 06 Whitney Wright Alpha Male XXX 1080p", "Alpha Male",
            site="Pure Taboo", performers=["Whitney Wright"], aired=datetime(2020, 10, 6))
weak = ev("Alpha Male 2020 WEB-DL", "Alpha Male", site=None)
check("full evidence outscores a bare title", strong.score > weak.score,
      f"{strong.score} vs {weak.score}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
