from scout import signals
from scout.market import load_market

FI = load_market("fi")
EE = load_market("ee")

GGMIIKO_BIO = "Jos tykkäät elektroniikasta, seuraa mua 😎🕹🇫🇮 📩 creator57c49a@example.fi Linkit👇"
NUVOO_AD = "*Mainos: @nuvoosuomi Pelitietokone todella edullisesti"


def test_emails_from_bios():
    assert signals.extract_emails("Techtok\nTech, News, Reviews\ncreatorbd06af@example.fi") == ["creatorbd06af@example.fi"]
    assert signals.extract_emails(GGMIIKO_BIO) == ["creator57c49a@example.fi"]
    assert signals.extract_emails("no contact here") == []


def test_market_signals_flag_city_word():
    ev = signals.market_signals("Eesti🇪🇪 Nr 1 Sisulooja, Tallinn", EE)
    assert "flag 🇪🇪" in ev
    assert "city Tallinn" in ev
    assert any("eesti" in e for e in ev)
    assert "flag 🇫🇮" in signals.market_signals(GGMIIKO_BIO, FI)
    assert signals.market_signals("just a gamer from texas", FI) == []


def test_domain_signal():
    assert "domain .fi" in signals.market_signals("mail: creatorbd06af@example.fi", FI)


def test_ad_and_sponsor_detection():
    assert signals.is_ad_caption(NUVOO_AD, FI)
    assert signals.sponsor_mentions(NUVOO_AD, FI) == ["nuvoosuomi"]
    assert signals.sponsor_mentions("hanging out with @friend", FI) == []
    assert signals.is_ad_caption("Reklaam | koostöös @brand", EE)


def test_competitor_matching_in_code():
    assert signals.competitor_hits([NUVOO_AD]) == ["Nuvoo"]
    assert signals.competitor_hits(["nothing relevant"]) == []


def test_business_hints():
    assert signals.business_hints("pctrade.fi", None, None)
    assert signals.business_hints("fartech.fi", "Fartech", "Verkkokauppa")
    assert signals.business_hints("digikamu", "Digikamu", "Techtok\nTech, News, Reviews\ncreatorbd06af@example.fi") == []


def test_links_from_bio():
    links = signals.extract_links("🎱🍒🫧\nMa hammustan\nIG: @illic7t\n💌Koostööd ja PR: creatord16dec@example.com")
    assert links["instagram"] == "illic7t"
    assert signals.extract_links("", ins_id="aguel_design")["instagram"] == "aguel_design"
