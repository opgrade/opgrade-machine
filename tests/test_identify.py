"""Catalog adapters, with HTTP mocked from real responses (shapes captured 2026-10-24)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from opgrade.api import create_app
from helpers import create_client, login
from opgrade.config import Settings
from opgrade.identify import (
    CardQuery,
    CatalogError,
    PokemonTcgCatalog,
    ScryfallCatalog,
    TcgdexCatalog,
    YgoProDeckCatalog,
)

POKEMON = {
    "data": [
        {"id": "base1-58", "name": "Pikachu", "number": "58", "rarity": "Common",
         "set": {"id": "base1", "name": "Base", "printedTotal": 102, "releaseDate": "1999/01/09"},
         "images": {"small": "https://images.pokemontcg.io/base1/58.png"}},
        {"id": "swsh4-43", "name": "Pikachu V", "number": "43", "rarity": "Rare Holo V",
         "set": {"name": "Vivid Voltage", "printedTotal": 185, "releaseDate": "2020/11/13"},
         "images": {"small": "https://images.pokemontcg.io/swsh4/43.png"}},
    ],
    "totalCount": 2,
}
SCRYFALL = {
    "data": [
        {"id": "aaa", "name": "Blacker Lotus", "set_name": "Unglued", "collector_number": "70",
         "released_at": "1998-08-11", "rarity": "rare", "lang": "en", "image_uris": {"small": "https://x/s1.jpg"}},
        {"id": "bbb", "name": "Black Lotus", "set_name": "Limited Edition Alpha", "collector_number": "232",
         "released_at": "1993-08-05", "rarity": "rare", "lang": "en", "image_uris": {"small": "https://x/s2.jpg"}},
        {"id": "ccc", "name": "Delver of Secrets // Insectile Aberration", "set_name": "Innistrad",
         "collector_number": "51", "released_at": "2011-09-30", "rarity": "common", "lang": "en",
         "card_faces": [{"image_uris": {"small": "https://x/s3.jpg"}}, {}]},
    ]
}
YGO = {
    "data": [
        {"id": 38033121, "name": "Dark Magician Girl",
         "card_sets": [
             {"set_name": "Magician's Force", "set_code": "MFC-000", "set_rarity": "Secret Rare"},
             {"set_name": "Collectible Tins 2005", "set_code": "CT2-EN004", "set_rarity": "Secret Rare"},
         ],
         "card_images": [{"image_url_small": "https://images.ygoprodeck.com/images/cards_small/38033121.jpg"}]}
    ]
}


TCGDEX_CARDS = {
    "en": {
        "charizard": [
            {"id": "base1-4", "localId": "4", "name": "Charizard", "image": "https://assets.tcgdex.net/en/base/base1/4"},
            {"id": "ex14-4", "localId": "4", "name": "Charizard δ", "image": "https://assets.tcgdex.net/en/ex/ex14/4"},
            {"id": "sm9-14", "localId": "14", "name": "Charizard", "image": "https://assets.tcgdex.net/en/sm/sm9/14"},
            {"id": "30th-c-001", "localId": "001", "name": "Charizard"},
            {"id": "A1-036", "localId": "036", "name": "Charizard", "image": "https://assets.tcgdex.net/en/tcgp/A1/036"},
        ],
    },
    "fr": {
        "dracaufeu": [{"id": "base1-4", "localId": "4", "name": "Dracaufeu", "image": "https://assets.tcgdex.net/fr/base/base1/4"}],
    },
}
TCGDEX_SETS = {"base1": ("Base Set", "1999-01-09"), "ex14": ("Crystal Guardians", "2006-08-30"),
               "sm9": ("Team Up", "2019-02-01"), "30th-c": ("30th Celebration", "2026-02-27"),
               "A1": ("Genetic Apex", "2024-10-30")}
TCGDEX_OFFICIAL = {"base1": 102, "ex14": 100, "sm9": 181}


def tcgdex_handler(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.split("/")  # ['', 'v2', lang, resource, ...]
    lang, resource = parts[2], parts[3]
    if resource == "cards":
        name = request.url.params["name"].lower()
        return httpx.Response(200, json=TCGDEX_CARDS.get(lang, {}).get(name, []))
    if resource == "series":  # only the digital series is ever requested
        return httpx.Response(200, json={"id": parts[4], "sets": [{"id": "A1", "name": "Genetic Apex"}]})
    if len(parts) == 4:
        return httpx.Response(200, json=[
            {"id": k, "name": v[0], "cardCount": {"official": TCGDEX_OFFICIAL.get(k)}} for k, v in TCGDEX_SETS.items()
        ])
    name, date = TCGDEX_SETS[parts[4]]
    return httpx.Response(200, json={"id": parts[4], "name": name, "releaseDate": date})


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_tcgdex_set_name_year_and_image():
    found = TcgdexCatalog(client_for(tcgdex_handler)).search(CardQuery("Charizard"))
    first = found[0]
    assert (first.external_id, first.name, first.set_name, first.number, first.year, first.language) == (
        "en:base1-4", "Charizard", "Base Set", "4", 1999, "en",
    )
    assert first.image_url == "https://assets.tcgdex.net/en/base/base1/4/low.webp"
    assert first.printed_number == "4/102"
    assert all(not c.external_id.startswith("en:A1-") for c in found)  # TCG Pocket: digital only
    promo = next(c for c in found if c.external_id == "en:30th-c-001")
    assert promo.set_name == "30th Celebration" and promo.image_url is None


def test_tcgdex_number_is_exact_not_substring():
    found = TcgdexCatalog(client_for(tcgdex_handler)).search(CardQuery("charizard", number="4/102"))
    assert {c.external_id for c in found} == {"en:base1-4", "en:ex14-4"}  # not sm9-14
    found = TcgdexCatalog(client_for(tcgdex_handler)).search(CardQuery("charizard", number="1"))
    assert [c.external_id for c in found] == ["en:30th-c-001"]  # "001" == "1"


def test_tcgdex_set_filter():
    found = TcgdexCatalog(client_for(tcgdex_handler)).search(CardQuery("charizard", set_name="base"))
    assert [c.external_id for c in found] == ["en:base1-4"]


def test_tcgdex_french_name_found_on_second_pass():
    found = TcgdexCatalog(client_for(tcgdex_handler)).search(CardQuery("Dracaufeu"))
    assert [(c.external_id, c.language, c.set_name) for c in found] == [("fr:base1-4", "fr", "Base Set")]


def test_pokemon_query_and_mapping():
    seen = {}

    def handler(request: httpx.Request):
        seen["q"] = request.url.params["q"]
        return httpx.Response(200, json=POKEMON)

    found = PokemonTcgCatalog(client_for(handler)).search(CardQuery("Pikachu", number="58/102", set_name="Base"))
    # Quoted names and punctuation make this API fail: words are joined with wildcards.
    assert seen["q"] == "name:pikachu* number:58 set.name:base*"
    first = found[0]
    assert (first.external_id, first.name, first.set_name, first.number, first.year) == (
        "base1-58", "Pikachu", "Base", "58/102", 1999,
    )
    assert first.image_url.startswith("https://")


def test_pokemon_name_punctuation_stripped():
    seen = {}

    def handler(request):
        seen["q"] = request.url.params["q"]
        return httpx.Response(200, json={"data": []})

    PokemonTcgCatalog(client_for(handler)).search(CardQuery('Mr. "Mime" é'))
    assert seen["q"] == "name:mr*mime*e*"


def test_pokemon_retries_transient_5xx_then_reports():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502, text="<html>bad gateway</html>")

    catalog = PokemonTcgCatalog(client_for(handler))
    catalog.retry_delay = 0
    with pytest.raises(CatalogError):
        catalog.search(CardQuery("Pikachu"))
    assert len(calls) == 4


def test_pokemon_recovers_after_transient_error():
    responses = iter([httpx.Response(500), httpx.Response(200, json=POKEMON)])
    catalog = PokemonTcgCatalog(client_for(lambda r: next(responses)))
    catalog.retry_delay = 0
    assert catalog.search(CardQuery("Pikachu"))[0].external_id == "base1-58"


def test_scryfall_exact_name_ranked_first_and_double_faced_image():
    found = ScryfallCatalog(client_for(lambda r: httpx.Response(200, json=SCRYFALL))).search(CardQuery("Black Lotus"))
    assert found[0].name == "Black Lotus" and found[0].year == 1993
    delver = next(c for c in found if c.external_id == "ccc")
    assert delver.image_url == "https://x/s3.jpg"


def test_scryfall_no_match_is_empty():
    body = {"object": "error", "code": "not_found", "status": 404}
    assert ScryfallCatalog(client_for(lambda r: httpx.Response(404, json=body))).search(CardQuery("zzz")) == []


def test_ygo_one_candidate_per_printing_and_number_filter():
    catalog = YgoProDeckCatalog(client_for(lambda r: httpx.Response(200, json=YGO)))
    assert len(catalog.search(CardQuery("dark magician girl"))) == 2
    found = catalog.search(CardQuery("dark magician girl", number="ct2-en004"))
    assert [c.external_id for c in found] == ["38033121:CT2-EN004"]


def test_ygo_no_match_is_empty():
    body = {"error": "No card matching your query was found in the database."}
    assert YgoProDeckCatalog(client_for(lambda r: httpx.Response(400, json=body))).search(CardQuery("zzz")) == []


def test_offline_raises_catalog_error():
    def handler(request):
        raise httpx.ConnectError("no network")

    catalog = YgoProDeckCatalog(client_for(handler))
    catalog.retry_delay = 0
    with pytest.raises(CatalogError, match="injoignable"):
        catalog.search(CardQuery("kuriboh"))


# -- API ------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    def offline(request):
        raise httpx.ConnectError("no network")

    catalogs = {
        "tcgdex": TcgdexCatalog(client_for(tcgdex_handler)),
        "scryfall": ScryfallCatalog(client_for(offline)),
    }
    for catalog in catalogs.values():
        catalog.retry_delay = 0
    with TestClient(create_app(Settings(simulation=True, data_dir=tmp_path), catalogs=catalogs)) as c:
        login(c)
        yield c


def _type_id(api, code):
    return next(t["id"] for t in api.get("/api/card-types").json() if t["code"] == code)


def test_identify_then_create_card_with_reference(api):
    r = api.get("/api/identify", params={"card_type_id": _type_id(api, "pokemon"), "name": "charizard"}).json()
    assert r["available"] and r["candidates"][0]["external_id"] == "en:base1-4"

    c = r["candidates"][0]
    client_id = create_client(api)["id"]
    card = api.post("/api/cards", json={
        "client_id": client_id, "card_type_id": _type_id(api, "pokemon"), "name": c["name"],
        "set_name": c["set_name"], "card_number": c["number"], "year": c["year"], "rarity": c["rarity"],
        "external_source": c["source"], "external_id": c["external_id"], "reference_image_url": c["image_url"],
    })
    assert card.status_code == 201 and card.json()["external_id"] == "en:base1-4"


def test_identify_sport_is_manual(api):
    r = api.get("/api/identify", params={"card_type_id": _type_id(api, "sport_hockey"), "name": "gretzky"}).json()
    assert not r["available"] and r["catalog"] is None and "manuelle" in r["message"]


def test_identify_offline_falls_back_to_manual(api):
    r = api.get("/api/identify", params={"card_type_id": _type_id(api, "magic"), "name": "black lotus"}).json()
    assert not r["available"] and "injoignable" in r["message"]


def test_card_rejects_unknown_catalog(api):
    client_id = create_client(api)["id"]
    r = api.post("/api/cards", json={"client_id": client_id, "card_type_id": 1, "name": "X", "external_source": "evil"})
    assert r.status_code == 422
