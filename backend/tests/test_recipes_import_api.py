"""/api/recipes/import, /import/files and /status, and the thumbnails a save
stores. Offline: the AI is the scripted FakeProvider, web pages and images
come from httpx.MockTransport, and every host resolves to a public address."""
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import media_lookup, recipe_ai, recipe_import, recipes
from app.api import recipes as recipes_api
from app.body_limit import MB, BodyLimitMiddleware
from app.overseer.provider import FakeProvider, ProviderReply
from app.recipe_models import RecipeIn

from conftest import make_client

AI_SETTINGS = dict(
    ai_provider="anthropic",
    anthropic_api_key=None,
    openai_api_key=None,
    google_api_key=None,
    anthropic_model=None,
    openai_model=None,
    google_model=None,
)


def ai_off(state) -> None:
    for name, value in AI_SETTINGS.items():
        setattr(state.settings, name, value)
    state.ai_config = {"provider": "anthropic"}


def ai_on(state, monkeypatch, *replies: str, stop: str = "end_turn") -> SimpleNamespace:
    """The AI configured, and every provider recipe_ai builds is one scripted
    FakeProvider. Returns it with `built`, the last build's arguments."""
    ai_off(state)
    state.settings.anthropic_api_key = "sk-ant-x"
    state.ai_config = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}}
    provider = FakeProvider(
        [ProviderReply(text=t, stop_reason=stop, usage={"input": 10, "output": 5}) for t in replies]
    )
    built: dict = {}

    def build(settings, **kwargs):
        built.update(kwargs)
        return provider

    monkeypatch.setattr(recipe_ai, "build_provider", build)
    return SimpleNamespace(provider=provider, built=built)


class Failing(FakeProvider):
    def __init__(self, exc):
        super().__init__([])
        self.exc = exc

    async def chat(self, *args, **kwargs):
        raise self.exc


def ai_failing(state, monkeypatch, status_code=529, name="InternalServerError") -> None:
    ai_on(state, monkeypatch)
    exc = type(name, (Exception,), {})("Overloaded")
    exc.status_code = status_code
    monkeypatch.setattr(recipe_ai, "build_provider", lambda settings, **kw: Failing(exc))


def ai_recipe(**overrides) -> dict:
    value = {
        "title": "Shakshuka",
        "base_servings": 2,
        "prep_minutes": 10,
        "cook_minutes": 20,
        "ingredients": [
            {"id": "eggs", "amount": 4, "unit": "", "item": "eggs", "prep": "",
             "scaling": "whole", "estimated": False, "group": None},
        ],
        "steps": [{"text": "Crack in {eggs}.", "timer_seconds": None}],
        "notes": "",
        "tags": ["breakfast"],
        "source_kind": "text",
        "missing": [],
    }
    value.update(overrides)
    return value


def prompt_of(provider, call=0) -> str:
    content = provider.seen[call][0]["content"]
    return content if isinstance(content, str) else content[-1]["text"]


def image_bytes(fmt="PNG") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 40)).save(buf, format=fmt)
    return buf.getvalue()


PDF = b"%PDF-1.4\n%fake\n"

RECIPE_LD = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Lemon Pasta",
    "image": "https://kitchen.example/img/lemon-pasta.jpg",
    "recipeYield": "4 servings",
    "recipeIngredient": ["400 g spaghetti", "2 lemons"],
    "recipeInstructions": [{"@type": "HowToStep", "text": "Boil 400 g spaghetti."}],
}
RECIPE_PAGE = (
    "<html><head><title>Lemon Pasta</title>"
    f'<script type="application/ld+json">{json.dumps(RECIPE_LD)}</script>'
    "</head><body><h1>Lemon Pasta</h1></body></html>"
)
PLAIN_PAGE = (
    "<html><head><title>Grandma's soup</title></head><body>"
    "<p>Grandma's soup: 1 onion, 2 carrots. Simmer for an hour.</p></body></html>"
)


def serve(monkeypatch, pages: dict[str, httpx.Response] | None = None, default=None) -> list[str]:
    """Answer page fetches from `pages` (URL -> response), else `default`,
    else a 404. Returns the URLs requested."""
    seen: list[str] = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        if pages and url in pages:
            return pages[url]
        return default(request) if default else httpx.Response(404)

    monkeypatch.setattr(
        recipe_import, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", public)
    return seen


def html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=body)


def client_for(state) -> TestClient:
    return make_client(state, recipes_api.router)


def post_import(client, **body):
    return client.post("/api/recipes/import", json=body)


# -- status ---------------------------------------------------------------------


def test_status_says_whether_the_ai_can_run(state, monkeypatch):
    client = client_for(state)
    ai_off(state)
    assert client.get("/api/recipes/status").json() == {"ai": False}
    ai_on(state, monkeypatch)
    assert client.get("/api/recipes/status").json() == {"ai": True}


# -- typed text -----------------------------------------------------------------

TYPED = "Pancakes\n\nIngredients\n2 cups flour\n1 egg\n\nMethod\nMix the flour and the egg."


def test_typed_text_without_ai_is_parsed_locally(state):
    ai_off(state)
    resp = post_import(client_for(state), text=TYPED)
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["method"] == "local"
    assert draft["recipe"]["title"] == "Pancakes"
    assert [i["item"] for i in draft["recipe"]["ingredients"]] == ["flour", "egg"]


def test_typed_text_with_ai_is_read_by_the_ai_at_medium_effort(state, monkeypatch):
    ai = ai_on(state, monkeypatch, json.dumps(ai_recipe()))
    resp = post_import(client_for(state), text=TYPED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["method"] == "ai" and resp.json()["recipe"]["title"] == "Shakshuka"
    assert "2 cups flour" in prompt_of(ai.provider)
    assert prompt_of(ai.provider).startswith("Convert this typed or pasted recipe text.")
    assert ai.built["reasoning"] == "medium"


def test_typed_text_falls_back_to_the_local_parser_when_the_ai_fails(state, monkeypatch):
    ai_failing(state, monkeypatch)
    resp = post_import(client_for(state), text=TYPED)
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["method"] == "local" and draft["recipe"]["title"] == "Pancakes"
    assert draft["missing"][0] == "The AI service is busy. Try again shortly. The text was read without AI."


def test_long_typed_text_is_cut_for_the_local_parser(state, monkeypatch):
    ai_off(state)
    monkeypatch.setattr(recipes_api, "MAX_LOCAL_TEXT", 40)
    draft = post_import(client_for(state), text=TYPED + "\n" + "x" * 100).json()
    assert draft["missing"][-1] == "The text was longer than 40 characters and was cut."


def test_nothing_to_import_is_a_400(state):
    ai_off(state)
    client = client_for(state)
    for body in ({}, {"text": "   "}, {"text": "", "url": " "}):
        resp = post_import(client, **body)
        assert resp.status_code == 400 and resp.json()["detail"] == "Paste a link or a recipe"


# -- links ------------------------------------------------------------------------


def test_a_recipe_website_is_read_from_its_schema_data(state, monkeypatch):
    ai_off(state)
    seen = serve(monkeypatch, {"https://kitchen.example/lemon-pasta": html(RECIPE_PAGE)})
    resp = post_import(client_for(state), url="https://kitchen.example/lemon-pasta")
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["method"] == "schema_org"
    recipe = draft["recipe"]
    assert recipe["title"] == "Lemon Pasta" and recipe["base_servings"] == 4
    assert recipe["source"] == {
        "type": "website",
        "url": "https://kitchen.example/lemon-pasta",
        "thumbnail": "https://kitchen.example/img/lemon-pasta.jpg",
    }
    assert seen == ["https://kitchen.example/lemon-pasta"]


def test_a_bare_link_gets_https_and_http_is_upgraded(state, monkeypatch):
    ai_off(state)
    seen = serve(monkeypatch, default=lambda request: html(RECIPE_PAGE))
    client = client_for(state)
    assert post_import(client, url="kitchen.example/lemon-pasta").status_code == 200
    assert post_import(client, url="  http://kitchen.example/b  ").status_code == 200
    assert seen == ["https://kitchen.example/lemon-pasta", "https://kitchen.example/b"]


def test_text_that_is_only_a_link_is_imported_as_one(state, monkeypatch):
    ai_off(state)
    seen = serve(monkeypatch, default=lambda request: html(RECIPE_PAGE))
    resp = post_import(client_for(state), text="  https://kitchen.example/lemon-pasta \n")
    assert resp.status_code == 200 and resp.json()["method"] == "schema_org"
    assert seen == ["https://kitchen.example/lemon-pasta"]
    # A link inside other text is text.
    assert post_import(client_for(state), text="see https://kitchen.example/x").json()["method"] == "local"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "url, label",
    [
        ("https://www.youtube.com/watch?v=abc", "YouTube"),
        ("youtu.be/abc", "YouTube"),
        ("https://www.instagram.com/reel/abc/", "Instagram"),
        ("https://www.tiktok.com/@cook/video/1", "TikTok"),
    ],
)
def test_video_links_are_a_later_phase(state, monkeypatch, url, label):
    ai_off(state)
    seen = serve(monkeypatch)
    resp = post_import(client_for(state), url=url)
    assert resp.status_code == 400
    assert resp.json()["detail"] == f"{label} links are not supported yet"
    assert seen == []


def test_a_page_without_recipe_data_goes_to_the_ai(state, monkeypatch):
    ai = ai_on(state, monkeypatch, json.dumps(ai_recipe(title="Grandma's Soup", source_kind="webpage")))
    serve(monkeypatch, {"https://soup.example/": html(PLAIN_PAGE)})
    resp = post_import(client_for(state), url="https://soup.example/")
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["method"] == "ai" and draft["recipe"]["title"] == "Grandma's Soup"
    assert draft["recipe"]["source"]["type"] == "website"
    assert draft["recipe"]["source"]["url"] == "https://soup.example/"
    prompt = prompt_of(ai.provider)
    assert prompt.startswith("This is the text of a web page.")
    assert "Source URL: https://soup.example/" in prompt and "Simmer for an hour." in prompt


def test_a_page_without_recipe_data_and_no_ai_is_a_400(state, monkeypatch):
    ai_off(state)
    serve(monkeypatch, {"https://soup.example/": html(PLAIN_PAGE)})
    resp = post_import(client_for(state), url="https://soup.example/")
    assert resp.status_code == 400 and resp.json()["detail"] == "This page has no recipe data"


def test_the_ai_failing_on_a_page_is_a_502(state, monkeypatch):
    ai_failing(state, monkeypatch, status_code=429, name="RateLimitError")
    serve(monkeypatch, {"https://soup.example/": html(PLAIN_PAGE)})
    resp = post_import(client_for(state), url="https://soup.example/")
    assert resp.status_code == 502
    assert resp.json()["detail"] == "The AI is rate limited. Try again in a minute."


def test_fetch_failures_are_short_errors(state, monkeypatch):
    ai_off(state)
    serve(
        monkeypatch,
        {
            # Cloudflare in front of Allrecipes and Serious Eats answers 403.
            "https://blocked.example/": httpx.Response(403, content=b"<html>Just a moment...</html>"),
            "https://pdf.example/": httpx.Response(200, headers={"content-type": "application/pdf"}, content=PDF),
        },
    )
    client = client_for(state)
    cases = [
        ("https://blocked.example/", 502, "Could not fetch the page (HTTP 403)"),
        ("https://missing.example/", 502, "Could not fetch the page (HTTP 404)"),
        ("https://pdf.example/", 415, "Not a web page"),
        ("ftp://kitchen.example/x", 400, "Links must be https"),
        ("https://kitchen .example/x", 400, "Not a valid link"),
    ]
    for url, status, detail in cases:
        resp = post_import(client, url=url)
        assert (resp.status_code, resp.json()["detail"]) == (status, detail), url


def test_a_link_to_a_private_host_is_refused(state, monkeypatch):
    ai_off(state)
    seen = serve(monkeypatch, default=lambda request: html(RECIPE_PAGE))

    async def lan(host):
        return ["192.168.1.10"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", lan)
    resp = post_import(client_for(state), url="router.example/admin")
    assert resp.status_code == 400 and resp.json()["detail"] == "Host is not public"
    assert seen == []


# -- photos and PDFs --------------------------------------------------------------


def post_files(client, files, text=None):
    data = {"text": text} if text is not None else None
    return client.post(
        "/api/recipes/import/files",
        files=[("files", f) for f in files],
        data=data,
    )


def test_photos_and_pdfs_are_read_as_one_recipe(state, monkeypatch):
    ai = ai_on(state, monkeypatch, json.dumps(ai_recipe()))
    resp = post_files(
        client_for(state),
        [
            ("page1.pdf", PDF, "application/pdf"),
            # A Blob appended without a name arrives as "blob".
            ("blob", image_bytes("JPEG"), "image/jpeg"),
        ],
        text="serves 2",
    )
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["method"] == "ai" and draft["recipe"]["source"]["type"] == "photo"
    content = ai.provider.seen[0][0]["content"]
    assert [b["type"] for b in content] == ["document", "image", "text"]
    assert content[0]["title"] == "page1.pdf"
    prompt = content[-1]["text"]
    assert "these files" in prompt and "pages of the same recipe" in prompt and "serves 2" in prompt
    assert ai.built["reasoning"] == "medium"


def test_files_need_the_ai(state):
    ai_off(state)
    resp = post_files(client_for(state), [("x.pdf", PDF, "application/pdf")])
    assert resp.status_code == 503
    assert resp.json()["detail"] == "No AI provider is configured."


def test_file_problems_are_400s_and_ai_failures_502s(state, monkeypatch):
    ai_on(state, monkeypatch, json.dumps(ai_recipe()))
    client = client_for(state)
    cases = [
        ([], 400, "Add a photo or a PDF."),
        ([("p.pdf", PDF, "application/pdf")] * 7, 400, "Send at most 6 files at once."),
        ([("x.pdf", image_bytes(), "application/pdf")], 400, "“x.pdf” is not a PDF."),
        ([("a.svg", b"<svg/>", "image/svg+xml")], 400, "“a.svg” is not a photo or a PDF."),
        ([("blob", b"<svg/>", "image/svg+xml")], 400, "The file is not a photo or a PDF."),
        ([("a.png", b"", "image/png")], 400, "The files were empty."),
    ]
    for files, status, detail in cases:
        resp = post_files(client, files)
        assert (resp.status_code, resp.json()["detail"]) == (status, detail), detail

    monkeypatch.setattr(recipe_ai, "MAX_IMAGE_BYTES", 16)
    resp = post_files(client, [("big.png", image_bytes(), "image/png")])
    assert (resp.status_code, resp.json()["detail"]) == (413, "“big.png” is larger than 0 MB.")

    ai_failing(state, monkeypatch)
    resp = post_files(client, [("p.pdf", PDF, "application/pdf")])
    assert (resp.status_code, resp.json()["detail"]) == (502, "The AI service is busy. Try again shortly.")


def test_an_oversized_upload_is_refused_at_the_limit(state, monkeypatch):
    """The files route has no cap of its own: the site-wide default (64 MB in
    main.py and in the Caddyfile) bounds it. Over the cap is a 413 before the
    route runs; under it the route reads the files."""
    ai = ai_on(state, monkeypatch, json.dumps(ai_recipe()))
    client = client_for(state)
    client.app.add_middleware(BodyLimitMiddleware, limits=[], default=4096)
    big = PDF + b"0" * 4096
    resp = post_files(client, [("big.pdf", big, "application/pdf")])
    assert resp.status_code == 413
    assert resp.json()["detail"] == "Request body is larger than 4 KB"
    assert ai.provider.seen == []


def test_the_files_route_is_under_the_site_wide_cap():
    from app import main as main_module

    [middleware] = [
        m for m in main_module.app.user_middleware if m.cls is BodyLimitMiddleware
    ]
    limit = BodyLimitMiddleware(
        None, middleware.kwargs["limits"], middleware.kwargs["default"]
    ).limit_for("/api/recipes/import/files")
    assert limit == middleware.kwargs["default"] == 64 * MB


# -- thumbnails a save stores ------------------------------------------------------

JPEG = image_bytes("JPEG")


def serve_images(monkeypatch, handler) -> list[str]:
    seen: list[str] = []

    def record(request):
        seen.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(
        media_lookup, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(record))
    )

    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", public)
    return seen


def jpeg(request):
    return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)


def with_thumbnail(title: str, thumbnail: str | None) -> dict:
    return RecipeIn(
        title=title, source={"type": "website", "url": "https://kitchen.example/x", "thumbnail": thumbnail}
    ).model_dump()


def test_a_remote_thumbnail_is_stored_as_an_attachment(state, monkeypatch):
    from app.api import attachments as attachments_api

    ai_off(state)
    seen = serve_images(monkeypatch, jpeg)
    client = make_client(state, recipes_api.router, attachments_api.router)
    resp = client.post("/api/recipes", json=with_thumbnail("Lemon Pasta", "https://kitchen.example/img/a.jpg"))
    assert resp.status_code == 201, resp.text
    thumbnail = resp.json()["source"]["thumbnail"]
    assert thumbnail.startswith("/api/attachments/lemon-pasta-") and thumbnail.endswith(".jpg")
    assert seen == ["https://kitchen.example/img/a.jpg"]
    assert f"thumbnail: {thumbnail}" in state.store.read(resp.json()["path"])
    assert client.get("/api/recipes").json()["recipes"][0]["thumbnail"] == thumbnail
    served = client.get(thumbnail)
    assert served.status_code == 200 and served.content == JPEG


def test_a_thumbnail_that_will_not_download_saves_the_recipe_without_one(state, monkeypatch):
    ai_off(state)
    serve_images(monkeypatch, lambda request: httpx.Response(403))
    resp = client_for(state).post("/api/recipes", json=with_thumbnail("Soup", "https://img.example/a.jpg"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["source"]["thumbnail"] is None
    assert "thumbnail" not in state.store.read(resp.json()["path"])

    # Not an image at all, the same.
    serve_images(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>"))
    resp = client_for(state).post("/api/recipes", json=with_thumbnail("Stew", "https://img.example/b.jpg"))
    assert resp.status_code == 201 and resp.json()["source"]["thumbnail"] is None


def test_thumbnails_that_are_neither_attachments_nor_https_are_dropped(state, monkeypatch):
    ai_off(state)
    seen = serve_images(monkeypatch, jpeg)
    client = client_for(state)
    for thumbnail in ("http://img.example/a.jpg", "data:image/jpeg;base64,AAAA", "javascript:alert(1)", "a.jpg"):
        resp = client.post("/api/recipes", json=with_thumbnail("Soup", thumbnail))
        assert resp.status_code == 201 and resp.json()["source"]["thumbnail"] is None, thumbnail
    kept = client.post("/api/recipes", json=with_thumbnail("Soup", "/api/attachments/soup-0123456789abcdef.jpg"))
    assert kept.json()["source"]["thumbnail"] == "/api/attachments/soup-0123456789abcdef.jpg"
    assert seen == []


def test_an_update_stores_a_new_remote_thumbnail_and_keeps_an_attachment(state, monkeypatch):
    ai_off(state)
    seen = serve_images(monkeypatch, jpeg)
    client = client_for(state)
    made = client.post("/api/recipes", json=with_thumbnail("Soup", None)).json()
    resp = client.put(
        f"/api/recipes/item/{made['path']}",
        json={"recipe": with_thumbnail("Soup", "https://img.example/soup.jpg"), "base_revision": made["revision"]},
    )
    assert resp.status_code == 200, resp.text
    stored = resp.json()["source"]["thumbnail"]
    assert stored.startswith("/api/attachments/soup-")
    assert seen == ["https://img.example/soup.jpg"]

    again = client.put(
        f"/api/recipes/item/{made['path']}",
        json={"recipe": with_thumbnail("Soup", stored), "base_revision": resp.json()["revision"]},
    )
    assert again.status_code == 200 and again.json()["source"]["thumbnail"] == stored
    assert len(seen) == 1

    # A stale revision is a 409 and a missing recipe a 404, before any download.
    stale = client.put(
        f"/api/recipes/item/{made['path']}",
        json={"recipe": with_thumbnail("Soup", "https://img.example/new.jpg"), "base_revision": "0" * 64},
    )
    assert stale.status_code == 409
    missing = client.put(
        "/api/recipes/item/recipes/Gone.md",
        json={"recipe": with_thumbnail("Gone", "https://img.example/gone.jpg")},
    )
    assert missing.status_code == 404
    assert len(seen) == 1


def test_the_service_layer_stores_thumbnails_for_every_creator(state, monkeypatch):
    """OMNIA's tools call recipes.create_recipe directly, not the route."""
    import asyncio

    ai_off(state)
    serve_images(monkeypatch, jpeg)
    made = asyncio.run(
        recipes.create_recipe(state, RecipeIn.model_validate(with_thumbnail("Toast", "https://img.example/t.jpg")))
    )
    assert made.source.thumbnail.startswith("/api/attachments/toast-")
