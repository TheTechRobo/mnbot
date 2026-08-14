from quart import Quart, abort, redirect, render_template, render_template_string, request, url_for
import werkzeug.exceptions
import os
import base64
import dataclasses
import datetime
import time
import json
import urlcanon

import sqlalchemy, sqlalchemy.ext.asyncio, sqlalchemy.dialects.postgresql
import aiohttp

from ..common import db, model

class EscapingQuart(Quart):
    def select_jinja_autoescape(self, filename: str) -> bool:
        return (not filename) or filename.endswith(".j2") or super().select_jinja_autoescape(filename)

app = EscapingQuart(__name__)
app.jinja_env.globals.update(isinstance = isinstance)

DOCUMENTATION_URL = os.getenv("DOCUMENTATION_URL")

NAV = (
    ("/", "Dashboard"),
    #("/claims", "Claims"),
    ("/pipelines", "Pipelines"),
    ("/docs", "Documentation"),
)

async def _setup_engine():
    global ENGINE
    ENGINE = await db.create_engine()

app.before_serving(_setup_engine)

@dataclasses.dataclass
class JobInfoPacket:
    id: model.UUID
    status: model.JobStatus
    active_claims: list[str]
    depth: int | None
    concurrency: int
    nice: int
    tag: str | None
    note: str | None
    initial_page: str
    ruleset: db.JobRuleset

@dataclasses.dataclass
class ResultsInfoPacket:
    screenshot: model.UUID | None = None
    cjs_screenshot: model.UUID | None = None
    custom_js_result: dict | None = None
    outlinks: list[str] | None = None
    requisites: list | None = None
    status_code: int | None = None
    final_url: str | None = None

@dataclasses.dataclass
class AttemptInfoPacket:
    id: model.UUID
    page_id: model.UUID
    pipeline_id: str
    pipeline_version: str
    error: str | None
    finished: bool
    ruleset: db.JobRuleset
    applied_settings: db.PageSettings
    results: ResultsInfoPacket

@dataclasses.dataclass
class PageInfoPacket:
    id: model.UUID
    job_id: model.UUID
    url: str
    status: model.PageStatus
    attempts: int
    attempts_remaining: int
    nice: int
    depth: int

    all_attempts: list[AttemptInfoPacket]

@app.context_processor
def aaa():
    return {"nav": NAV, "len": len}

@app.route("/")
async def home():
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        q = (
            sqlalchemy.select(model.jobs.c.job_id, model.jobs.c.initial_page, model.jobs.c.note)
            .where(model.jobs.c.status.in_((model.JobStatus.ACTIVE, model.JobStatus.DRAINING)))
            .order_by(*model.jobs_dequeue_order)
        )
        active_jobs = (await conn.execute(q)).all()
    return await render_template("home.j2", jobs = active_jobs)

@app.route("/docs")
async def docs():
    if DOCUMENTATION_URL:
        return redirect(DOCUMENTATION_URL, 302)
    return await render_template("error.j2", reason = "No documentation URL", description = "The DOCUMENTATION_URL environment variable was not set. Please report this!"), 500

@app.route("/item/translate")
async def translate_form_input():
    if "item" not in request.args:
        abort(400)
    id = request.args['item']
    try:
        id = db.parse_id(id)
    except db.InvalidIdError:
        url = db.Connection.canon_for_ssurt(id)
        if url.host.startswith(b"*."):
            # Only include the host, for a wildcard search
            search = urlcanon.ssurt_host(url.host.removeprefix(b"*."))
        else:
            search = db.Connection.ssurt(id)
        return redirect(url_for("search_url", ssurt = search))
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        q = sqlalchemy.select(
            sqlalchemy.exists(sqlalchemy.select(model.jobs).where(model.jobs.c.job_id == id)),
            sqlalchemy.exists(sqlalchemy.select(model.pages).where(model.pages.c.page_id == id))
        )
        res = await conn.execute(q)
        is_job, is_page = res.one()
    if is_job:
        if is_page:
            return await render_template("error.j2", reason = "Ambiguous ID", description = "The ID you provided was found as both a job ID and a page ID. Please report this!"), 500
        return redirect(url_for("single_job", job_id = id))
    elif is_page:
        return redirect(url_for("single_page", page_id = id))
    return await render_template("error.j2", reason = "No such ID", description = "No job or page was found with the provided ID."), 404

def route_with_json(route, **kwargs):
    """
    Adds app.route for route and route + ".json".
    The callback should take an argument called html, which indicates whether or not to return HTML.
    """
    assert "defaults" not in kwargs
    def inner(cb):
        html_cb = app.route(
            route,
            defaults = {"html": True},
            **kwargs
        )(cb)
        return app.route(
            route + ".json",
            defaults = {"html": False},
            **kwargs
        )(html_cb)
    return inner

@route_with_json("/page/<page_id>")
async def single_page(page_id, html):
    q = (
            sqlalchemy.select(model.pages, db.Connection._page_depth(page_id).label("depth"), db.Connection._job_depth(model.pages.c.job_id).label("job_depth"))
            .where(model.pages.c.page_id == page_id)
    )
    attempt_q = (
        sqlalchemy.select(model.attempts, model.job_rulesets.c.job_ruleset_id, *model.job_ruleset_columns)
        .select_from(model.attempts)
        .join(model.job_rulesets, model.attempts.c.ruleset_id == model.job_rulesets.c.job_ruleset_id)
        .where(model.attempts.c.page_id == page_id)
    )

    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        res = await conn.execute(q)
        row = res.one_or_none()
        if row is None:
            if html:
                return await render_template("error.j2", reason = f"Page ID {page_id} not found", description = f"No page with this ID exists.", show_item_search = True), 404
            return {"status": 404, "message": "Page ID not found"}, 404
        page_packet = PageInfoPacket(page_id, row.job_id, row.payload, row.status, row.attempts, row.attempts_remaining, row.nice, row.depth, [])
        job_depth = row.job_depth

        attempts_res = await conn.execute(attempt_q)
        for row in attempts_res:
            # TODO: Flatten this into the attempt_q query.
            results_q = sqlalchemy.select(model.results).where(model.results.c.attempt_id == row.attempt_id)
            results_r = await conn.execute(results_q)
            results = ResultsInfoPacket()
            for result in results_r:
                match result.type:
                    case model.ResultType.CUSTOM_JS_SCREENSHOT:
                        results.cjs_screenshot = result.result_id
                    case model.ResultType.FINAL_URL:
                        results.final_url = result.payload
                    case model.ResultType.OUTLINKS:
                        results.outlinks = result.payload
                    case model.ResultType.REQUISITES:
                        results.requisites = result.payload
                    case model.ResultType.SCREENSHOT:
                        results.screenshot = result.result_id
                    case model.ResultType.STATUS_CODE:
                        results.status_code = result.payload
                    case model.ResultType.CUSTOM_JS:
                        results.custom_js_result = result.payload
            ruleset = db.JobRuleset.from_row(row)
            applied_settings = db.PageSettings.from_ruleset(page_packet.url, ruleset)
            page_packet.all_attempts.append(AttemptInfoPacket(
                id = row.attempt_id,
                page_id = page_id,
                pipeline_id = row.pipeline_id,
                pipeline_version = row.pipeline_version,
                error = row.error,
                finished = row.finished,
                ruleset = ruleset,
                applied_settings = applied_settings,
                results = results,
            ))
    if html:
        return await render_template("page.j2", page = page_packet, job_depth = job_depth)
    v = dataclasses.asdict(page_packet)
    v['status'] = v['status'].name
    return {"status": 200, "page": v, "job_depth": job_depth}

@route_with_json("/job/<job_id>")
async def single_job(job_id, html):
    claim_q = (
        sqlalchemy.select(sqlalchemy.dialects.postgresql.array_agg(sqlalchemy.text("claims.*")))
        .where(model.claims.c.job_id == job_id)
        .scalar_subquery()
    )
    ruleset_q = (
        sqlalchemy.select(model.job_rulesets.c.job_ruleset_id, *model.job_ruleset_columns)
        .where(model.job_rulesets.c.job_id == job_id)
        .order_by(model.job_rulesets.c.job_ruleset_id.desc())
        .limit(1)
        .subquery()
    )

    q = (
        sqlalchemy.select(
            model.jobs,
            claim_q.label("all_claims"),
            ruleset_q,
        )
        .select_from(model.jobs)
        .where(model.jobs.c.job_id == job_id)
        .join(ruleset_q, sqlalchemy.true(), isouter = True)
    )
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        res = await conn.execute(q)
        row = res.one_or_none()
        if row is None:
            if html:
                return await render_template("error.j2", reason = f"Job ID {job_id} not found", description = "No job with this ID exists.", show_item_search = True), 404
            return {"status": 404, "message": "Job ID not found"}, 404
        ruleset = db.JobRuleset.from_row(row)
        packet = JobInfoPacket(
            id = row.job_id,
            status = row.status,
            depth = row.depth,
            concurrency = row.concurrency,
            nice = row.nice,
            tag = row.tag,
            note = row.note,
            initial_page = row.initial_page,
            ruleset = ruleset,
            active_claims = row.all_claims or [],
        )
    if html:
        timestamp = datetime.datetime.fromtimestamp(db.parse_id_ex(packet.id).timestamp / 1000, datetime.timezone.utc)
        return await render_template("job.j2", job = packet, timestamp = timestamp.isoformat(sep = " ", timespec = "seconds"))
    v = dataclasses.asdict(packet)
    v['status'] = v['status'].name
    return {"status": 200, "job": v}

async def pages_list(q, html, list_template, volatile = True, include_header = False, ugly_hack = None):
    page_size = 10
    try:
        offset = int(request.args.get("offset", 0))
        assert offset >= 0
    except (ValueError, AssertionError):
        if html:
            return await render_template("error.j2", reason = "Bad request", description = "Offset parameter was invalid."), 400
        return {"status": 400, "error": "Offset parameter was invalid."}
    q = q.offset(offset).limit(page_size)

    rows = []
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        res = await conn.stream(q)
        async for row in res:
            rows.append(row._asdict())

    next_offset = None
    prev_offset = max(offset - page_size, 0) if offset > 0 else None
    if len(rows) >= page_size:
        next_offset = offset + page_size
    if html:
        return await render_template("list/" + list_template, rows = rows, offset = offset, next_offset = next_offset, prev_offset = prev_offset, volatile = volatile, include_header = include_header, ugly_hack = ugly_hack)
    return {"status": 200, "rows": rows, "next": next_offset, "prev": prev_offset}

pages_q = lambda job_id : (
    sqlalchemy.select(model.pages.c.page_id, model.pages.c.payload, sqlalchemy.func.count(model.attempts.c.page_id).label("attempt_count"))
    .select_from(model.pages)
    .join(model.attempts, model.attempts.c.page_id == model.pages.c.page_id, isouter = True)
    .where(model.pages.c.job_id == job_id)
    .group_by(model.pages.c.page_id)
    .order_by(*model.pages_dequeue_order)
)

@route_with_json("/job/<job_id>/pending")
async def job_pending(job_id, html):
    q = (
        pages_q(job_id)
        .where(model.pages_dequeue_filter)
        .where(
            (db.Connection._page_depth(model.pages.c.page_id) <= db.Connection._job_depth(job_id))
            | (db.Connection._job_depth(job_id) == None)
        )
    )
    return await pages_list(q, html, "pages_with_attempts.j2")

@route_with_json("/job/<job_id>/claimed")
async def job_claimed(job_id, html):
    q = pages_q(job_id).where(model.pages.c.status == model.PageStatus.CLAIMED)
    return await pages_list(q, html, "pages_with_attempts.j2")

@route_with_json("/job/<job_id>/pages")
async def job_pages(job_id, html):
    q = (
        sqlalchemy.select(model.pages.c.page_id, model.pages.c.payload, model.pages.c.status, model.pages.c.attempts_remaining)
        .select_from(model.pages)
        .where(model.pages.c.job_id == job_id)
        .order_by(model.pages.c.page_id)
    )
    return await pages_list(q, html, "pages_with_status.j2", volatile = False)

@route_with_json("/search")
async def search_url(html):
    ssurt = request.args['ssurt']
    q = (
        sqlalchemy.select(model.pages.c.page_id, model.pages.c.payload, sqlalchemy.func.uuid_extract_timestamp(model.pages.c.page_id).label("date"))
        .where(model.pages.c.payload_ssurt.startswith(ssurt))
    )
    return await pages_list(q, html, "pages_with_date.j2", volatile = False, include_header = True, ugly_hack = f"Using ssurt prefix {ssurt}")

@route_with_json("/ruleset/<job_id>/<ruleset_id>/test")
async def test_ruleset(job_id, ruleset_id, html):
    url = request.args['url']
    normalized = str(urlcanon.whatwg(url))
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        queue = db.Connection(conn)
        latest_ruleset = await queue.get_job_ruleset(job_id)
        warning = "<p><b>Warning: You are not querying the latest ruleset.</b></p>" if str(latest_ruleset.job_ruleset_id) != ruleset_id else ""
        if url != normalized:
            warning += "<p>Warning: When extracting outlinks, this URL will be normalized to <code>{{ normalized|e }}</code>.</p>"
        ruleset = await queue.get_ruleset(ruleset_id)
        settings = db.PageSettings.from_ruleset(url, ruleset)
    if html:
        return await render_template_string(
            warning + 'URL: <code>{{ url }}</code> <br /> {% import "macros.j2" as macros %} {{ macros.build_settings(settings, true) }}',
            settings = settings,
            url = url,
            normalized = normalized,
        )
    return {"status": 200, "settings": settings}

@app.route("/page/<id>/requisites")
async def requisites(id):
    return get_requisites(id), {"content-type": "application/json"}

async def get_requisites(page_id):
    yield "["
    q = (
            sqlalchemy.select(model.results.c.payload)
            .select_from(model.attempts)
            .join(model.results, model.results.c.attempt_id == model.attempts.c.attempt_id)
            .where(model.attempts.c.page_id == page_id)
            .where(model.results.c.type == model.ResultType.REQUISITES)
    )
    started = False
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        async with conn.stream(q) as iter:
            async for row in iter:
                result = row[0]
                for requisite in result:
                    for entry in requisite['chain']:
                        if req := entry['request']:
                            if req['url'].startswith("http"):
                                if started:
                                    yield ", "
                                started = True
                                yield json.dumps(req['url'])
    yield "]"

@app.route("/page/<id>/outlinks")
async def outlinks(id):
    return get_outlinks(id), {"content-type": "application/json"}

async def get_outlinks(page_id):
    yield "["
    q = (
            sqlalchemy.select(model.results.c.payload)
            .select_from(model.attempts)
            .join(model.results, model.results.c.attempt_id == model.attempts.c.attempt_id)
            .where(model.attempts.c.page_id == page_id)
            .where(model.results.c.type == model.ResultType.OUTLINKS)
    )
    started = False
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        async with conn.stream(q) as iter:
            async for row in iter:
                result = row[0]
                for outlink in result:
                    if started:
                        yield ", "
                    started = True
                    yield json.dumps(outlink)
    yield "]"

@app.route("/screenshot/<id>.jpg")
async def screenshot(id):
    q = sqlalchemy.select(model.results.c.payload).where(model.results.c.result_id == id)
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        res = await conn.scalar(q)
    if not res:
        return await render_template("error.j2", code = 404, reason = "Screenshot not found", description = "Screenshot was not found."), 404
    return base64.b85decode(res), {"Content-Type": "image/jpeg"}

@route_with_json("/pipelines")
async def pipelines(html):
    async with ENGINE.connect() as conn:
        conn = await conn.execution_options(postgresql_readonly = True)
        queue = db.Connection(conn)
        pipelines = await queue.get_pipelines()
        res = []
        for pipeline in pipelines:
            if health := pipeline.pipeline_health:
                heartbeat_delta = datetime.datetime.now(datetime.UTC) - health.last_checkin
                heartbeat_delta_dict = {"days": heartbeat_delta.days, "seconds": heartbeat_delta.seconds}
                heartbeat_delta_dict['healthy'] = heartbeat_delta < queue.MAX_HEARTBEAT_AGE
                disk = (round(health.disk_free_bytes / 1024 / 1024 / 1024, 1), round(health.disk_total_bytes / 1024 / 1024 / 1024, 1), health.disk_free_bytes > queue.MIN_FREE_BYTES)
                res.append((pipeline.pipeline_id, heartbeat_delta_dict, disk, pipeline.matchonly))
            else:
                res.append((pipeline.pipeline_id, None, None, pipeline.matchonly))
    if html:
        return await render_template("pipelines.j2", pipelines = res)
    return {"status": 200, "pipelines": pipelines}

@app.errorhandler(werkzeug.exceptions.HTTPException)
async def error(e: werkzeug.exceptions.HTTPException):
    if request.accept_mimetypes.accept_json:
        return await render_template("error.j2", code = e.code, reason = e.name, description = e.description), e.code
    return {"status": e.code, "message": f"{e.name}: {e.description}"}, e.code

