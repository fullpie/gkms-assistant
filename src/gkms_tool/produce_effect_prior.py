"""Small shared category prior from Master effect names; no visual readers."""
from collections.abc import Iterable


def rank_produce_effect_categories(effect_types: Iterable[str]) -> tuple[int, str] | None:
    joined = " ".join(effect_types)
    priorities = (
        ("ProduceCardUpgrade", 1000, "card-upgrade"),
        ("ProduceCardDuplicate", 950, "card-duplicate"),
        ("AuditionNpcWeaken", 900, "audition-weaken"),
        ("VoteCountAddition", 850, "vote"),
        ("ProduceCard", 820, "deck"),
        ("Attribute", 800, "attribute"),
        ("ProduceDrink", 750, "drink"),
        ("StaminaRecover", 700, "stamina"),
        ("ProducePointAddition", 600, "produce-point"),
    )
    return next(((score, reason) for token, score, reason in priorities if token in joined), None)
