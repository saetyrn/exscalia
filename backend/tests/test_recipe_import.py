"""Recipe website import: the guarded page fetch (offline, on a real httpx
client over MockTransport), link classification, and the schema.org reader."""
import asyncio
import json
import time

import httpx
import pytest

from app import media_lookup, recipe_import
from app.recipe_import import (
    RecipeImportError,
    classify_url,
    draft_from_schema,
    find_recipe,
    iso_duration_minutes,
)

RECIPE_LD = {
    "@context": "https://schema.org",
    "@graph": [
        {"@type": "WebSite", "@id": "https://kitchen.example/#website", "name": "Example Kitchen"},
        {
            "@type": "WebPage",
            "@id": "https://kitchen.example/lemon-pasta/",
            "image": {"@id": "https://kitchen.example/lemon-pasta/#primaryimage"},
        },
        {
            "@type": "Recipe",
            "@id": "https://kitchen.example/lemon-pasta/#recipe",
            "name": "Lemon Pasta",
            "image": [
                "https://kitchen.example/wp-content/uploads/lemon-pasta-1x1.jpg",
                "https://kitchen.example/wp-content/uploads/lemon-pasta-4x3.jpg",
            ],
            "recipeYield": ["4", "4 servings"],
            "prepTime": "PT10M",
            "cookTime": "PT15M",
            "totalTime": "PT25M",
            "recipeIngredient": ["400 g spaghetti", "2 lemons", "60 g butter", "Salt, to taste"],
            "recipeInstructions": [
                {
                    "@type": "HowToSection",
                    "name": "Pasta",
                    "itemListElement": [
                        {"@type": "HowToStep", "text": "Boil 400 g spaghetti for 9 minutes."}
                    ],
                },
                {
                    "@type": "HowToSection",
                    "name": "Sauce",
                    "itemListElement": [
                        {"@type": "HowToStep", "text": "Melt 60 g butter with the zest of 2 lemons."},
                        {"@type": "HowToStep", "text": "Toss everything together."},
                    ],
                },
            ],
            "recipeCategory": ["Dinner"],
            "recipeCuisine": ["Italian"],
            "keywords": "pasta, lemon, quick",
        },
    ],
}

PAGE = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="/style.css">
<title>Lemon Pasta - Example Kitchen</title>
<script type="application/ld+json">{json.dumps(RECIPE_LD)}</script>
<style>body {{ color: red }}</style>
</head>
<body><nav>Home</nav>
<article><h1>Lemon Pasta</h1><p>Bright and quick.<br>Serves four.
<ul><li>400 g spaghetti<li>2 lemons</ul></article>
<script>var hidden = "tracking";</script>
</body></html>"""


def use_pages(monkeypatch, handler, resolve=None) -> list[str]:
    """Route the fetch through `handler` on a real AsyncClient; every host
    resolves to a public address unless `resolve` says otherwise. Returns the
    URLs actually requested."""
    seen: list[str] = []

    def record(request):
        seen.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(
        recipe_import,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )

    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", resolve or public)
    return seen


def html_page(request, body=PAGE, **headers):
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", **headers}, content=body)


def fetch(url):
    return asyncio.run(recipe_import.fetch(url))


def fetch_error(url) -> RecipeImportError:
    with pytest.raises(RecipeImportError) as info:
        fetch(url)
    return info.value


# -- fetch ----------------------------------------------------------------------


def test_fetch_reads_ld_json_text_and_title(monkeypatch):
    use_pages(monkeypatch, html_page)
    page = fetch("https://kitchen.example/lemon-pasta/")
    assert page.url == "https://kitchen.example/lemon-pasta/"
    assert page.title == "Lemon Pasta - Example Kitchen"
    assert [b.get("@context") for b in page.blocks] == ["https://schema.org"]
    # Body text survives void tags in <head>; scripts and styles stay out.
    assert "Bright and quick." in page.text and "400 g spaghetti" in page.text
    assert "tracking" not in page.text and "color: red" not in page.text

    draft = draft_from_schema(find_recipe(page.blocks), page.url)
    assert draft.recipe.title == "Lemon Pasta"
    assert draft.recipe.source.url == "https://kitchen.example/lemon-pasta/"


def test_fetch_follows_checked_redirects(monkeypatch):
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/new"})
        if request.url.path == "/new":
            return httpx.Response(302, headers={"location": "https://cdn.kitchen.example/final"})
        return html_page(request)

    seen = use_pages(monkeypatch, handler)
    page = fetch("https://kitchen.example/old")
    assert seen == [
        "https://kitchen.example/old",
        "https://kitchen.example/new",
        "https://cdn.kitchen.example/final",
    ]
    assert page.url == "https://cdn.kitchen.example/final"


def test_fetch_refuses_plain_http(monkeypatch):
    seen = use_pages(monkeypatch, html_page)
    err = fetch_error("http://kitchen.example/lemon-pasta/")
    assert (err.status, err.detail) == (400, "Links must be https")
    assert seen == []


def test_fetch_refuses_private_addresses(monkeypatch):
    seen = use_pages(monkeypatch, html_page)
    for literal in (
        "https://127.0.0.1/recipe",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/recipe",
        "https://10.0.0.8/recipe",
    ):
        err = fetch_error(literal)
        assert (err.status, err.detail) == (400, "Host is not public")

    async def lan(host):
        return ["93.184.216.34", "192.168.1.20"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", lan)
    assert fetch_error("https://sneaky.example/recipe").detail == "Host is not public"
    assert seen == []


def test_fetch_refuses_localhost_with_the_real_resolver(monkeypatch):
    real_resolver = media_lookup._resolve_addresses
    seen = use_pages(monkeypatch, html_page, resolve=real_resolver)
    assert fetch_error("https://localhost/recipe").detail == "Host is not public"
    assert seen == []


def test_fetch_refuses_a_redirect_to_a_private_address(monkeypatch):
    def handler(request):
        if request.url.host == "kitchen.example":
            return httpx.Response(302, headers={"location": "https://internal.example/admin"})
        return html_page(request)

    async def resolve(host):
        return ["10.0.0.5"] if host == "internal.example" else ["93.184.216.34"]

    seen = use_pages(monkeypatch, handler, resolve=resolve)
    err = fetch_error("https://kitchen.example/recipe")
    assert (err.status, err.detail) == (400, "Host is not public")
    assert seen == ["https://kitchen.example/recipe"]


@pytest.mark.parametrize(
    "location, detail",
    [
        ("https://169.254.169.254/latest/meta-data", "Host is not public"),
        ("http://kitchen.example/recipe", "Links must be https"),
        # Not IDNA: httpx raises a UnicodeError reading the host, which was a 500.
        ("https://xn--localhost/recipe", "Could not fetch the page"),
    ],
)
def test_fetch_refuses_bad_redirect_targets(monkeypatch, location, detail):
    seen = use_pages(monkeypatch, lambda request: httpx.Response(302, headers={"location": location}))
    assert fetch_error("https://kitchen.example/start").detail == detail
    assert seen == ["https://kitchen.example/start"]


def test_a_link_whose_host_is_not_idna_is_a_website_and_refused():
    assert classify_url("xn--localhost/recipe") == "website"
    assert fetch_error("https://xn--localhost/recipe").status == 400


def test_fetch_caps_redirects(monkeypatch):
    seen = use_pages(monkeypatch, lambda request: httpx.Response(302, headers={"location": f"/{len(seen)}"}))
    err = fetch_error("https://kitchen.example/start")
    assert (err.status, err.detail) == (502, "Too many redirects")
    assert len(seen) == recipe_import.PAGE_MAX_REDIRECTS + 1


def test_fetch_refuses_an_oversized_page(monkeypatch):
    big = b"<html>" + b"x" * (recipe_import.PAGE_MAX_BYTES + 1)
    use_pages(monkeypatch, lambda request: html_page(request, body=big))
    err = fetch_error("https://kitchen.example/huge")
    assert (err.status, err.detail) == (413, "The page is too large")


def test_fetch_stops_reading_a_streamed_page_at_the_cap(monkeypatch):
    sent = []

    async def chunks():
        for _ in range(1000):
            sent.append(1)
            yield b"x" * 65536

    use_pages(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=chunks()),
    )
    err = fetch_error("https://kitchen.example/endless")
    assert err.status == 413
    assert len(sent) <= recipe_import.PAGE_MAX_BYTES // 65536 + 2


@pytest.mark.parametrize("encoding", ["gzip, gzip", "gzip,deflate", "deflate, gzip, gzip"])
def test_fetch_refuses_stacked_content_encodings(monkeypatch, encoding):
    """httpx decodes every layer of a chunk in one call, before the size cap
    sees a byte: a few KB of gzip inside gzip is gigabytes. One layer is
    bounded (about 1,000 to 1) and still reads."""
    import gzip

    bomb = gzip.compress(gzip.compress(b"\0" * (64 * 1024 * 1024), 9), 9)
    decoded = []
    real_decode = httpx._decoders.GZipDecoder.decode
    monkeypatch.setattr(
        httpx._decoders.GZipDecoder,
        "decode",
        lambda self, data: decoded.append(1) or real_decode(self, data),
    )
    async def streamed():  # as off the network: nothing decodes it but the reader
        yield bomb

    use_pages(monkeypatch, lambda request: html_page(request, body=streamed(), **{"content-encoding": encoding}))
    err = fetch_error("https://kitchen.example/bomb")
    assert (err.status, err.detail) == (502, "Could not fetch the page")
    assert decoded == []

    gzipped = gzip.compress(PAGE.encode())
    use_pages(monkeypatch, lambda request: html_page(request, body=gzipped, **{"content-encoding": "gzip"}))
    assert fetch("https://kitchen.example/lemon-pasta/").title == "Lemon Pasta - Example Kitchen"


@pytest.mark.parametrize("ctype", ["application/pdf", "image/jpeg", "application/octet-stream"])
def test_fetch_refuses_non_html_content(monkeypatch, ctype):
    use_pages(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": ctype}, content=b"%PDF-1.7"))
    err = fetch_error("https://kitchen.example/recipe.pdf")
    assert (err.status, err.detail) == (415, "Not a web page")


def test_fetch_without_a_content_type_sniffs_for_markup(monkeypatch):
    use_pages(monkeypatch, lambda request: httpx.Response(200, content=b"%PDF-1.7 binary"))
    assert fetch_error("https://kitchen.example/file").status == 415

    use_pages(monkeypatch, lambda request: httpx.Response(200, content=b"\n<html><body><p>Hi</p></body></html>"))
    assert fetch("https://kitchen.example/page").text == "Hi"


def test_fetch_reports_http_and_transport_failures(monkeypatch):
    use_pages(monkeypatch, lambda request: httpx.Response(404, headers={"content-type": "text/html"}))
    err = fetch_error("https://kitchen.example/gone")
    assert (err.status, err.detail) == (502, "Could not fetch the page (HTTP 404)")

    def down(request):
        raise httpx.ConnectError("connection refused to https://kitchen.example/?token=secret")

    use_pages(monkeypatch, down)
    err = fetch_error("https://kitchen.example/recipe")
    assert (err.status, err.detail) == (502, "Could not fetch the page")

    async def unresolvable(host):
        raise OSError("nodename nor servname provided")

    seen = use_pages(monkeypatch, html_page, resolve=unresolvable)
    err = fetch_error("https://nowhere.example/recipe")
    assert (err.status, err.detail) == (502, "Site not found")
    assert seen == []


def test_fetch_has_one_deadline(monkeypatch):
    async def trickle():
        for _ in range(50):
            await asyncio.sleep(0.01)
            yield b"<p>x</p>"

    use_pages(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=trickle()),
    )
    monkeypatch.setattr(recipe_import, "PAGE_TIMEOUT", 0.05)
    err = fetch_error("https://kitchen.example/slow")
    assert (err.status, err.detail) == (504, "The page took too long")


@pytest.mark.parametrize("url", ["not a link", "", "https://", "kitchen.example/recipe"])
def test_fetch_refuses_malformed_links(monkeypatch, url):
    seen = use_pages(monkeypatch, html_page)
    assert fetch_error(url).status == 400
    assert seen == []


def test_fetch_decodes_the_declared_charset(monkeypatch):
    latin = "<html><body><p>Crème brûlée</p></body></html>".encode("latin-1")
    use_pages(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "text/html; charset=ISO-8859-1"}, content=latin),
    )
    assert fetch("https://kitchen.example/a").text == "Crème brûlée"

    meta = '<html><head><meta charset="windows-1252"></head><body><p>Jalapeño</p></body></html>'.encode("cp1252")
    use_pages(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=meta))
    assert fetch("https://kitchen.example/b").text == "Jalapeño"


def test_fetch_text_survives_sloppy_and_hostile_markup(monkeypatch):
    sloppy = "<html><head><title>T</title><body><p>One<p>Two<ul><li>Three</ul><script>x()</script>Four</body>"
    use_pages(monkeypatch, lambda request: html_page(request, body=sloppy))
    assert fetch("https://kitchen.example/sloppy").text.split() == ["One", "Two", "Three", "Four"]

    hostile = "<body>" + "<div>" * 100_000 + "deep" + "</span>" * 100_000
    use_pages(monkeypatch, lambda request: html_page(request, body=hostile))
    started = time.monotonic()
    assert fetch("https://kitchen.example/hostile").text == "deep"
    assert time.monotonic() - started < 5


def test_fetch_reads_lenient_ld_json(monkeypatch):
    body = (
        '<html><head><script type="application/ld+json">//<![CDATA[\n'
        '{"@type": "Recipe", "name": "Line\nbreak"}\n//]]></script>'
        '<script type="application/ld+json">{not json}</script>'
        '<script type="application/ld+json">[{"@type": "Recipe", "name": "Two"}, "junk", 3]</script>'
        "</head><body></body></html>"
    )
    use_pages(monkeypatch, lambda request: html_page(request, body=body))
    page = fetch("https://kitchen.example/lenient")
    assert [b["name"] for b in page.blocks] == ["Line\nbreak", "Two"]


# -- classify_url -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url, kind",
    [
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://m.youtube.com/shorts/abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("youtube.com/watch?v=abc", "youtube"),
        ("https://www.instagram.com/reel/abc/", "reel"),
        ("https://vm.tiktok.com/abc/", "tiktok"),
        ("https://www.tiktok.com/@cook/video/1", "tiktok"),
        ("https://www.seriouseats.com/lemon-pasta", "website"),
        ("https://notyoutube.com/watch", "website"),
        ("https://kitchen.example/youtube.com", "website"),
        ("", "website"),
        ("https://[::1", "website"),
    ],
)
def test_classify_url(url, kind):
    assert classify_url(url) == kind


# -- find_recipe ----------------------------------------------------------------------


def test_find_recipe_shapes():
    recipe = {"@type": "Recipe", "name": "Soup"}
    assert find_recipe([recipe]) is recipe
    assert find_recipe([{"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, recipe]}]) is recipe
    listed = {"@type": ["Recipe", "NewsArticle"], "name": "Listed"}
    assert find_recipe([{"@type": "Organization"}, listed]) is listed
    assert find_recipe([{"@type": "WebPage", "mainEntity": recipe}]) is recipe
    assert find_recipe([{"@type": "ItemList", "itemListElement": [[recipe]]}]) is recipe
    prefixed = {"@type": "http://schema.org/Recipe", "name": "Prefixed"}
    assert find_recipe([prefixed]) is prefixed
    assert find_recipe([{"@type": "schema:Recipe"}]) == {"@type": "schema:Recipe"}
    assert find_recipe([{"@type": "WebPage"}, {"@type": "RecipeCollection"}]) is None
    assert find_recipe([]) is None


# -- draft_from_schema -----------------------------------------------------------------


def test_iso_duration():
    assert iso_duration_minutes("PT1H30M") == 90
    assert iso_duration_minutes("PT20M") == 20
    assert iso_duration_minutes("P1DT2H") == 1560
    assert iso_duration_minutes(None) is None


@pytest.mark.parametrize(
    "value, minutes",
    [
        ("pt45m", 45),
        (" PT1H ", 60),
        ("PT1.5H", 90),
        ("PT0H25M0S", 25),
        ("P0Y0M0DT0H35M0S", 35),
        ("PT30S", 1),
        ("P20M", 20),  # minutes without the T, as some sites write it
        ("1 hour 30 minutes", 90),
        ("25 mins", 25),
        ("", None),
        ("soon", None),
        (35, None),
        ("P" + "9" * 400 + "D", None),
    ],
)
def test_iso_duration_real_world(value, minutes):
    assert iso_duration_minutes(value) == minutes


def test_schema_org_draft():
    data = {
        "@type": "Recipe",
        "name": "Lemon pasta",
        "recipeYield": ["4", "4 servings"],
        "prepTime": "PT10M",
        "totalTime": "PT30M",
        "image": {"@type": "ImageObject", "url": "https://x/img.jpg"},
        "recipeIngredient": ["400 g spaghetti", "2 lemons", "1/2 cup parmesan, grated", "Salt"],
        "recipeInstructions": [
            {"@type": "HowToStep", "text": "Cook 400 g spaghetti for 9 minutes."},
            {"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "Toss with the lemon."}]},
        ],
        "recipeCategory": "Dinner",
        "keywords": "pasta, quick",
    }
    d = draft_from_schema(data, "https://x/recipe")
    r = d.recipe
    assert r.title == "Lemon pasta" and r.base_servings == 4
    assert (r.prep_minutes, r.cook_minutes) == (10, 20)
    assert r.source.thumbnail == "https://x/img.jpg" and r.source.type == "website"
    assert len(r.ingredients) == 4 and len(r.steps) == 2
    assert "{spaghetti}" in r.steps[0].text and r.steps[0].timer_seconds == 540
    assert r.tags == ["dinner", "pasta", "quick"]
    assert d.method == "schema_org"


def test_graph_recipe_with_sections():
    data = find_recipe([RECIPE_LD])
    d = draft_from_schema(data, "https://kitchen.example/lemon-pasta/")
    r = d.recipe
    assert r.title == "Lemon Pasta" and r.base_servings == 4
    assert (r.prep_minutes, r.cook_minutes) == (10, 15)
    assert [s.text for s in r.steps] == [
        "Boil {spaghetti} for 9 minutes.",
        "Melt {butter} with the zest of {lemons}.",
        "Toss everything together.",
    ]
    assert r.steps[0].timer_seconds == 540
    assert r.ingredients[-1].scaling == "to_taste"
    assert r.tags == ["dinner", "italian", "pasta", "lemon", "quick"]
    assert r.source.thumbnail == "https://kitchen.example/wp-content/uploads/lemon-pasta-1x1.jpg"
    assert d.missing == []


@pytest.mark.parametrize(
    "instructions, steps",
    [
        ("Boil the water.\nAdd the pasta.<br>Drain it.", ["Boil the water.", "Add the pasta.", "Drain it."]),
        ("<p>Heat the oven.</p><p>Bake for 20 minutes.</p>", ["Heat the oven.", "Bake for 20 minutes."]),
        ("Mix well. Rest it. then serve", ["Mix well.", "Rest it. then serve"]),
        (["Chop the onion.", "Fry it gently."], ["Chop the onion.", "Fry it gently."]),
        (
            [
                {"@type": "HowToStep", "text": "<b>Whisk</b>&nbsp;the eggs."},
                {"@type": "HowToStep", "name": "Season to taste."},
                {"@type": "HowToStep", "text": "<p></p>"},
            ],
            ["Whisk the eggs.", "Season to taste."],
        ),
        (
            [
                {
                    "@type": ["HowToSection"],
                    "name": "Dough",
                    "itemListElement": [{"@type": "HowToStep", "text": "Knead."}],
                },
                {"@type": "HowToSection", "name": "Filling", "itemListElement": ["Stir the filling."]},
            ],
            ["Knead.", "Stir the filling."],
        ),
        ({"@type": "HowToStep", "text": "One step only."}, ["One step only."]),
        (None, []),
    ],
)
def test_instruction_shapes(instructions, steps):
    d = draft_from_schema({"@type": "Recipe", "name": "X", "recipeInstructions": instructions}, "https://x/")
    assert [s.text for s in d.recipe.steps] == steps
    assert ("No steps in the page data." in d.missing) == (not steps)


@pytest.mark.parametrize(
    "yield_, servings",
    [
        ("4 servings", 4),
        ("Makes 12", 12),
        (6, 6),
        (["8"], 8),
        ("Serves 4-6", 4),
        ({"@type": "QuantitativeValue", "value": 3}, 3),
        ("250 cookies", 100),
    ],
)
def test_yields(yield_, servings):
    d = draft_from_schema({"@type": "Recipe", "name": "X", "recipeYield": yield_}, "https://x/")
    assert d.recipe.base_servings == servings
    assert not any("Servings" in m for m in d.missing)


@pytest.mark.parametrize("yield_", [None, "a few", 0, float("nan"), ""])
def test_missing_yield_defaults_to_four(yield_):
    d = draft_from_schema({"@type": "Recipe", "name": "X", "recipeYield": yield_}, "https://x/")
    assert d.recipe.base_servings == 4
    assert "Servings not found; set to 4." in d.missing


@pytest.mark.parametrize(
    "image, thumbnail",
    [
        ("https://cdn.x/a.jpg", "https://cdn.x/a.jpg"),
        ("http://cdn.x/a.jpg", None),
        ("//cdn.x/a.jpg", "https://cdn.x/a.jpg"),
        ("/uploads/a.jpg", "https://x/uploads/a.jpg"),
        (["http://cdn.x/a.jpg", {"@type": "ImageObject", "url": "https://cdn.x/b.jpg"}], "https://cdn.x/b.jpg"),
        ({"@type": "ImageObject", "contentUrl": "https://cdn.x/c.jpg"}, "https://cdn.x/c.jpg"),
        ({"@id": "https://x/#primaryimage"}, None),
        ("data:image/png;base64,AAAA", None),
        ("javascript:alert(1)", None),
        (7, None),
    ],
)
def test_thumbnail_is_https_only(image, thumbnail):
    d = draft_from_schema({"@type": "Recipe", "name": "X", "image": image}, "https://x/recipe")
    assert d.recipe.source.thumbnail == thumbnail


def test_title_entities_and_fallback():
    d = draft_from_schema({"@type": "Recipe", "name": "Mom&#39;s &amp; Pop&#8217;s <em>Pie</em>"}, "https://x/")
    assert d.recipe.title == "Mom's & Pop’s Pie"
    d = draft_from_schema({"@type": "Recipe", "name": "<b> </b>"}, "https://x/")
    assert d.recipe.title == "Untitled recipe" and "No title in the page data." in d.missing


def test_total_time_only_is_stored_as_cook_time():
    d = draft_from_schema({"@type": "Recipe", "name": "X", "totalTime": "PT40M"}, "https://x/")
    assert (d.recipe.prep_minutes, d.recipe.cook_minutes) == (None, 40)
    assert "Total time stored as cook time." in d.missing


def test_ingredient_html_and_a_single_string():
    d = draft_from_schema(
        {"@type": "Recipe", "name": "X", "recipeIngredient": "1 &frac12; cups <b>flour</b>\n2 eggs"},
        "https://x/",
    )
    assert [(i.amount, i.unit, i.item) for i in d.recipe.ingredients] == [(1.5, "cup", "flour"), (2, "", "eggs")]


@pytest.mark.parametrize(
    "data",
    [
        {"@type": "Recipe"},
        {
            "name": 5,
            "recipeYield": float("inf"),
            "prepTime": ["PT5M"],
            "recipeIngredient": [None, 3, "", "<br>"],
            "recipeInstructions": {"@type": "HowToStep", "text": None},
            "image": {"url": None},
            "keywords": 4,
        },
        {"name": "x" * 500, "recipeYield": "9" * 5000, "recipeIngredient": {"a": 1}},
    ],
)
def test_draft_from_schema_never_raises(data):
    d = draft_from_schema(data, "https://x/")
    assert d.recipe.title and 1 <= d.recipe.base_servings <= 100


# -- hostile pages: the reader stays linear and capped ------------------------------
# A page is up to PAGE_MAX_BYTES of the author's choosing, and the API and
# OMNIA both run this reader on whatever it holds. Each shape below took
# minutes to hours before the reader was bounded (measured: 10 s for a
# 400,000-item @graph, 14 s for 4,000 ingredients and steps, 6 s for 20,000
# characters of em spaces, and quadratic in each).


def quick(fn, limit=3.0):
    started = time.monotonic()
    out = fn()
    assert time.monotonic() - started < limit
    return out


def test_find_recipe_is_linear_in_a_huge_graph():
    blocks = [{"@graph": [{} for _ in range(400_000)] + [{"@type": "Recipe", "name": "Last"}]}]
    assert quick(lambda: find_recipe(blocks))["name"] == "Last"


@pytest.mark.parametrize(
    "data",
    [
        # every ingredient against every step, 1.6 MB of them
        {
            "recipeIngredient": [f"1 g item{i}" for i in range(40_000)],
            "recipeInstructions": [{"text": f"Add 1 g item{i}."} for i in range(40_000)],
        },
        # a whitespace run the step-linking pattern used to try every split of
        {"recipeIngredient": ["1 g q"], "recipeInstructions": "1" + "\u2003" * 1_000_000 + "z"},
        {"recipeIngredient": ["1 g q"], "recipeInstructions": ["1" + "\x0c" * 1_000_000 + "z"]},
        # tags that never close
        {"name": "<" * 1_000_000, "recipeIngredient": ["<br" * 300_000], "recipeInstructions": "<br" * 300_000},
        # digit runs in the times
        {"prepTime": "1" * 1_000_000 + "x", "cookTime": "P" + "1" * 1_000_000, "recipeYield": "1" * 1_000_000},
        # nesting, and a million images that are not https
        {"recipeInstructions": json.loads("[" * 500 + '"Stir."' + "]" * 500)},
        {"image": ["javascript:x"] * 300_000},
    ],
)
def test_draft_from_schema_is_bounded_on_hostile_data(data):
    d = quick(lambda: draft_from_schema({"@type": "Recipe", "name": "Hostile", **data}, "https://x/"))
    assert len(d.recipe.title) <= 200
    text = sum(len(i.item) + len(i.prep) for i in d.recipe.ingredients) + sum(len(s.text) for s in d.recipe.steps)
    assert text <= recipe_import.MAX_SCHEMA_TEXT


def test_draft_from_schema_says_when_it_cut_the_recipe():
    data = {
        "@type": "Recipe",
        "name": "Long",
        "recipeIngredient": [f"{i} g item {i}" for i in range(1, 2_000)],
        "recipeInstructions": [f"Step {i}." for i in range(1, 20_000)],
    }
    d = draft_from_schema(data, "https://x/")
    assert d.recipe.ingredients[0].item == "item 1" and d.recipe.steps
    assert any("cut" in m for m in d.missing)
    # An ordinary recipe is never cut.
    d = draft_from_schema(RECIPE_LD["@graph"][2], "https://x/")
    assert not any("cut" in m for m in d.missing)

