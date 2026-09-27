import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_snapshot import drop_brand_ties  # noqa: E402

BRAND = re.compile("acme", re.I)


def test_brand_deal_phrases_are_removed():
    assert drop_brand_ties("Regular Minecraft creator and a recent PR with Acme; suitable for partnership.",
                           BRAND) == "Regular Minecraft creator; suitable for partnership."
    assert drop_brand_ties("CS2 streamer; a caption mentions a ladder with Acme.", BRAND) == "CS2 streamer."
    assert drop_brand_ties("Sponsored by Acme, plays Fortnite.", BRAND) == "Plays Fortnite."


def test_plain_brand_mentions_stay():
    text = "The audience is a direct match for Acme's target market."
    assert drop_brand_ties(text, BRAND) == text
