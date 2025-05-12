from quart import Quart, abort, redirect, render_template, request, url_for
import werkzeug.exceptions

class EscapingQuart(Quart):
    def select_jinja_autoescape(self, filename: str) -> bool:
        return filename.endswith(".j2") or super().select_jinja_autoescape(filename)

from rue import Queue

QUEUE = Queue("mnbot")

app = EscapingQuart(__name__)
app.jinja_env.globals.update(isinstance = isinstance)

NAV = (
    ("/", "Dashboard"),
    ("/queue", "Pending"),
    ("/claims", "Claims"),
)

app.before_serving(QUEUE.check)

@app.context_processor
def aaa():
    return {"nav": NAV, "len": len}

@app.route("/")
async def home():
    status = await QUEUE.counts()
    return await render_template("home.j2", status = status.counts)

@app.route("/item/translate")
async def translate_form_input():
    if "item" not in request.args:
        abort(400)
    return redirect(url_for("single_item", id = request.args['item']), 301)

def route_with_json(route, **kwargs):
    """
    Adds app.route for route and route + ".json".
    The callback should take an argument called html, which indicates whether or not to return HTML.
    """
    assert "defaults" not in kwargs
    def inner(cb):
        json_cb = app.route(
            route + ".json",
            defaults = {"html": False},
            **kwargs
        )(cb)
        return app.route(
            route,
            defaults = {"html": True},
            **kwargs
        )(json_cb)
    return inner

@route_with_json("/queue")
async def pending(html):
    pending = await QUEUE.pending()
    if html:
        return await render_template("pending.j2", pending = pending, adj = "Pending")
    return {"status": 200, "pending": [i.as_json_friendly_dict() for i in pending]}

@route_with_json("/claims")
async def claims(html):
    claims = QUEUE.claimed()
    if html:
        return await render_template("pending.j2", pending = claims, adj = "Claimed")
    claims = []
    async for item in QUEUE.claimed():
        claims.append(item)
    return {"status": 200, "claims": claims}

@route_with_json("/item/<id>")
async def single_item(id, html):
    item = await QUEUE.get(id)
    if not item:
        if html:
            return await render_template("error.j2", reason = "Item not found", description = f"Item {id} was not found.", show_item_search = True), 404
        return {"status": 404, "message": "Item not found."}, 404
    results = {}
    for attempt in range(len(item.attempts)):
        results[attempt] = []
    async for result in QUEUE.get_results(item):
        results[result.attempt].append(result)
    results = dict(sorted(results.items()))
    if html:
        return await render_template("item.j2", item = item, results = results)
    return {"status": 200, "item": item.as_json_friendly_dict(), "results": results}

@app.route("/screenshot/<id>/<key>.jpg")
async def screenshot(id, key):
    if key not in ("full", "thumb"):
        abort(400)
    result = await QUEUE.get_result(id)
    if not result:
        abort(404)
    if not result.data[key] and key == "thumb":
        # Thumbnail didn't work. Send the full image instead.
        key = "full"
    return result.data[key], {"Content-Type": "image/jpeg"}

@app.errorhandler(werkzeug.exceptions.HTTPException)
async def error(e: werkzeug.exceptions.HTTPException):
    if request.accept_mimetypes.accept_json:
        return await render_template("error.j2", code = e.code, reason = e.name, description = e.description), e.code
    return {"status": e.code, "message": f"{e.name}: {e.description}"}, e.code

