"""AVN's categories folded into ten broad groups.

AVN has awarded ~620 distinct category names across forty ceremonies, mostly
renames of the same award ("Best Anal-Themed Feature", "Best Anal Release",
"Best Anal Movie or Limited Series", ...). Browsing them one by one is useless,
so every category is placed in one of ten groups. Story-driven films come
first because they are what is most worth finding; the rest are there for
browsing.

Matching is by keyword, in ``_RULES`` order, which is NOT the display order:
the specific themes run before the broad ones so that "Best All-Girl Feature"
lands in All-Girl rather than in Story because it says "Feature", and "Best
Foreign Feature" in International. A category no rule claims goes to Specialty
& Niche -- never to Story, which must stay what it says.

Checked against the live corpus's 615 winner category strings: 84 winners
fell through to the fallback before the bestseller and Hot Video rules were
added, nearly all sales charts and companies (the companies are now not
stored at all -- see `avn.NON_MEDIA_CATEGORY`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Group:
    key: str
    name: str
    description: str


#: Display order: story first.
GROUPS: tuple[Group, ...] = (
    Group("story", "Story & Drama", "Best movie, drama, feature and screenplay winners."),
    Group("comedy", "Comedy & Parody", "Comedies, parodies and the clever-title award."),
    Group(
        "production",
        "Production Value",
        "Cinematography, directing, editing, music, effects and technical awards.",
    ),
    Group("all-girl", "All-Girl", "All-girl movies and girl/girl scenes."),
    Group("group", "Group & Gangbang", "Group, orgy, gangbang and three-way scenes."),
    Group("anal-oral", "Anal & Oral", "Anal, oral and double-penetration movies and scenes."),
    Group("boy-girl", "Boy/Girl & Solo", "Couples, boy/girl, POV and solo scenes."),
    Group("gonzo", "Gonzo, Amateur & POV", "Gonzo, all-sex, vignette, pro-am and POV releases."),
    Group(
        "specialty",
        "Specialty & Niche",
        "MILF, taboo, interracial, trans, fetish and every other niche.",
    ),
    Group("international", "International", "Foreign and international productions."),
)

BY_KEY = {group.key: group for group in GROUPS}


def _r(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


# Craft awards go to a person for a film; only the film is ever stored (see
# `avn.CRAFT_AWARD`), and these words place it.
_CRAFT = (
    r"cinematograph|videograph|direct(?:or|ing)|edit(?:ing|or)|\bmusic\b|"
    r"musical score|original song|soundtrack|art direction|make ?up|"
    r"special effects|authoring"
)

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Writing is the story, not the production.
    ("story", _r(r"screenplay|\bwriting\b|\bwriter\b")),
    (
        "production",
        _r(
            _CRAFT
            + r"|high.definition|\bhd\b|blu-?ray|dvd extras|dvd menus|interactive|"
            r"virtual reality|\bvr\b|\b3-?d\b|technical"
        ),
    ),
    ("international", _r(r"foreign|international|european|hot video award")),
    ("comedy", _r(r"comedy|parod|clever title|humou?r")),
    ("all-girl", _r(r"all-girl|all girl|girl/girl|girl-girl|lesbian")),
    (
        "group",
        _r(
            r"group|orgy|orgies|gang ?bang|blowbang|three-?way|3-way|foursome|"
            r"tag-team|tag team|airtight|multi-partner"
        ),
    ),
    ("anal-oral", _r(r"\banal\b|\boral\b|double.penetration|blowjob|deep ?throat")),
    (
        "specialty",
        _r(
            r"special|fetish|fem-?dom|strap-on|bd/?sm|bondage|spanking|niche|big butt|"
            r"big bust|squirt|milf|mixed-age|older woman|taboo|interracial|ethnic|"
            r"young girl|ing[ée]nue|trans|tranny|trannie|she-?male|gay|bisexual|"
            r"alternative|internal|outrageous|kink|\bfeet\b|\bfoot\b|softcore|"
            r"animated|educational|curve appeal|polyamory"
        ),
    ),
    ("boy-girl", _r(r"boy/girl|couples?|\bsolo\b|masturbat|tease|sex scene|\bscene\b")),
    (
        "gonzo",
        _r(
            r"gonzo|cinemacore|all-sex|all sex|wall-to-wall|vignette|pro-am|amateur|"
            r"exhibitionist|\bpov\b|continuing|compilation|compendium|anthology|"
            r"series|non-feature|free-form|channel|featurette|explicit"
        ),
    ),
    (
        "story",
        _r(
            r"drama|\bfilm\b|feature|picture|movie|romance|thriller|action|"
            r"mainstream|narrative|classic|\bepic\b|propositions|^best dvd$|"
            # The sales charts: the year's best-selling and most-rented films.
            r"(?:top|best) (?:selling|renting)"
        ),
    ),
)


def group_of(category: str | None) -> str:
    """The key of the group this award category belongs to."""

    if not category:
        return "specialty"

    for key, rule in _RULES:
        if rule.search(category):
            return key

    return "specialty"
