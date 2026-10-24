import cv2
import httpx
import numpy as np
import pytest

from opgrade.identify import CardCandidate, CatalogError, YgoProDeckCatalog
from opgrade.identify.ocr import NumberReader, number_matches, read_tokens
from opgrade.identify.recognize import CardRecognizer
from opgrade.identify.visual import VisualIndex, fingerprint
from test_visual import card_picture  # pytest puts tests/ on sys.path


@pytest.mark.parametrize(
    "text,catalog,printed,expected",
    [
        ("Illus. Mitsuhiro Arita ©1999 Wizards. 4/102 ★", "tcgdex", "4/102", True),
        ("Illus. Mitsuhiro Arita ©1999 Wizards. 4/102 ★", "tcgdex", "4/130", False),
        ("SSP EN 009 / 191", "tcgdex", "9/191", True),  # spaces and leading zeros
        ("SSPEN 009/191", "tcgdex", "009/191", True),
        ("LV.76 #6 -30", "tcgdex", "6/102", False),
        ("GG01/GG70 ©2023", "tcgdex", "GG01", False),  # printed with its subset total
        ("0123 R BLB • EN", "scryfall", "123", True),
        ("123/350 R", "scryfall", "123", True),
        ("LOB-ENOO5 1st Edition", "ygoprodeck", "LOB-EN005", True),  # O misread for 0
        ("LOB - EN005", "ygoprodeck", "LOB-EN005", True),
        ("LOB-EN005", "ygoprodeck", "SDK-001", False),
    ],
)
def test_number_matching(text, catalog, printed, expected):
    assert number_matches(printed, read_tokens(text, catalog), catalog) is expected


def test_no_printed_number_never_matches():
    assert not number_matches(None, {"4/102"}, "tcgdex")


@pytest.fixture(scope="module")
def reader():
    r = NumberReader()
    if not r.available:
        pytest.skip("RapidOCR not installed")
    return r


def test_real_ocr_reads_number_at_machine_resolution(reader):
    card = cv2.resize(card_picture(5), (630, 880))
    card[800:870, :] = (235, 235, 230)
    # ~2.5 mm high digits at 10 px/mm, as on a real card bottom line.
    cv2.putText(card, "Illus. Someone   58/102", (20, 845), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (20, 20, 20), 2, cv2.LINE_AA)
    tokens = read_tokens(reader.read(card, "tcgdex"), "tcgdex")
    assert number_matches("58/102", tokens, "tcgdex")


# -- recognizer -----------------------------------------------------------------------------


class FakeReader:
    available = True

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def read(self, card_bgr, catalog):
        self.calls += 1
        return self.text


def reprint_index(tmp_path) -> VisualIndex:
    index = VisualIndex(tmp_path / "idx.db")
    same_art = fingerprint(card_picture(1))
    index.add_many([
        (CardCandidate("tcgdex", "en:base1-4", "Charizard", "Base Set", "4", 1999, printed_number="4/102"), same_art),
        (CardCandidate("tcgdex", "en:base4-4", "Charizard", "Base Set 2", "4", 2000, printed_number="4/130"), same_art),
        (CardCandidate("tcgdex", "en:other-9", "Pikachu", "Other", "9", 2001, printed_number="9/50"),
         fingerprint(card_picture(2))),
    ])
    return index


def no_catalog(_):
    raise AssertionError("no catalog call expected")


def test_ocr_settles_reprints(tmp_path):
    reader = FakeReader("Illus. Mitsuhiro Arita ©1999 Wizards. 4/130 ★")
    result = CardRecognizer(reprint_index(tmp_path), reader, no_catalog).recognize(card_picture(1), "tcgdex")
    assert result.candidates[0]["external_id"] == "en:base4-4"
    assert result.candidates[0]["number_match"] is True
    assert "4/130" in result.message


def test_unreadable_number_keeps_reprint_warning(tmp_path):
    result = CardRecognizer(reprint_index(tmp_path), FakeReader("Illus."), no_catalog).recognize(card_picture(1), "tcgdex")
    assert "Plusieurs éditions" in result.message and "non lu" in result.message
    assert not any(c["number_match"] for c in result.candidates)


def test_no_ocr_when_artwork_is_unique(tmp_path):
    reader = FakeReader("9/50")
    result = CardRecognizer(reprint_index(tmp_path), reader, no_catalog).recognize(card_picture(2), "tcgdex")
    assert reader.calls == 0  # OCR only runs when needed (it costs seconds on the Pi)
    assert result.candidates[0]["external_id"] == "en:other-9"


YGO = {"data": [{
    "id": 46986414, "name": "Dark Magician",
    "card_sets": [
        {"set_name": "Legend of Blue Eyes White Dragon", "set_code": "LOB-005", "set_rarity": "Ultra Rare"},
        {"set_name": "Starter Deck: Yugi", "set_code": "SDY-006", "set_rarity": "Ultra Rare"},
    ],
    "card_images": [{"image_url_small": "https://x/dm.jpg"}],
}]}


def ygo_index(tmp_path) -> VisualIndex:
    index = VisualIndex(tmp_path / "idx.db")
    index.add_many([(CardCandidate("ygoprodeck", "art:46986414", "Dark Magician", image_url="https://x/dm.jpg"),
                     fingerprint(card_picture(3)))])
    return index


def test_ygo_printing_found_from_set_code(tmp_path):
    catalog = YgoProDeckCatalog(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=YGO))))
    result = CardRecognizer(ygo_index(tmp_path), FakeReader("SDY-006"), lambda _: catalog).recognize(
        card_picture(3), "ygoprodeck")
    best = result.candidates[0]
    assert (best["set_name"], best["number"], best["number_match"]) == ("Starter Deck: Yugi", "SDY-006", True)
    assert best["image_url"] == "https://x/dm.jpg"


def test_ygo_offline_keeps_artwork(tmp_path):
    def offline(_):
        class Down:
            def search(self, q):
                raise CatalogError("YGOPRODeck injoignable (hors ligne ?).")
        return Down()

    result = CardRecognizer(ygo_index(tmp_path), FakeReader("SDY-006"), offline).recognize(card_picture(3), "ygoprodeck")
    assert result.candidates[0]["external_id"] == "art:46986414"
    assert "choisir l'édition" in result.message
