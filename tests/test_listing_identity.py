from app.listing_identity import listing_identity


def key(**changes):
    return listing_identity({"title": "Sony Walkman", "price": 4.0, "currency": "USD",
                             "location": "Located in Japan", **changes})["fingerprint"]


def test_identity_normalizes_formatting_without_using_ebay_id():
    assert key() == key(title="  SONY  Walkman  ", price=4, location="From Japan", ebay_id="123456789001")
    assert key(price="4.00") == key()
    assert key(title="Ｓｏｎｙ Walkman") == key()


def test_identity_changes_for_title_price_location_and_currency():
    assert len({key(), key(title="Sony radio"), key(price=5), key(location="Canada"), key(currency="EUR")}) == 5


def test_unknown_price_uses_visible_price_text():
    assert key(price=None, price_text="Contact seller") == key(price=None, price_text=" CONTACT  SELLER ")
    assert key(price=None, price_text="Auction") != key(price=None, price_text="Contact seller")
