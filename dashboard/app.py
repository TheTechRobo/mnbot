from quart import Quart, render_template, request
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
    ("/admin", "Admin")
)

app.before_serving(QUEUE.check)

@app.context_processor
def aaa():
    return {"nav": NAV, "len": len}

@app.route("/")
async def home():
    status = await QUEUE.counts()
    return await render_template("home.j2", status = status.counts)

@app.route("/queue")
async def pending():
    pending = await QUEUE.pending()
    if request.accept_mimetypes.accept_html:
        return await render_template("pending.j2", pending = pending)
    return {"status": 200, "pending": [i.as_json_friendly_dict() for i in pending]}

@app.route("/item/<id>")
async def single_item(id):
    item = await QUEUE.get(id)
    if not item:
        if request.accept_mimetypes.accept_html:
            return await render_template("error.j2", reason = "Item not found", description = f"Item {id} was not found."), 404
        return {"status": 404, "message": "Item not found."}, 404
    results = {}
    for attempt in range(len(item.attempts)):
        results[attempt] = []
    async for result in QUEUE.get_results(item):
        results[result.attempt].append(result)
    results = dict(sorted(results.items()))
    if request.accept_mimetypes.accept_html:
        return await render_template("item.j2", item = item, results = results)
    return {"status": 200, "item": item.as_json_friendly_dict(), "results": results}

@app.errorhandler(werkzeug.exceptions.HTTPException)
async def error(e: werkzeug.exceptions.HTTPException):
    if request.accept_mimetypes.accept_json:
        return await render_template("error.j2", code = e.code, reason = e.name, description = e.description), e.code
    return {"status": e.code, "message": f"{e.name}: {e.description}"}, e.code

