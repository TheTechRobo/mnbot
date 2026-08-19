import asyncio
import datetime
import os
import dataclasses

import sqlalchemy, sqlalchemy.ext.asyncio, sqlalchemy.exc, sqlalchemy.dialects.postgresql.asyncpg
import asyncpg

import pytest
import pytest_asyncio

from tracker.common import db, model
from tracker.scripts.__main__ import add_pipeline, create

@dataclasses.dataclass
class CreateArgs:
    uri: str
    tries: int

@dataclasses.dataclass
class AddPipelineArgs:
    uri: str
    id: str
    matchonly: bool

CREATED = asyncio.Event()

phase_report_key = pytest.StashKey[dict[str, pytest.CollectReport]]()
# https://docs.pytest.org/en/latest/example/simple.html#making-test-result-information-available-in-fixtures
# Allows fixtures to tell whether the test passed - currently unused, but may be useful
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    rep = yield

    # store test results for each phase of a call, which can
    # be "setup", "call", "teardown"
    item.stash.setdefault(phase_report_key, {})[rep.when] = rep

    return rep

@pytest_asyncio.fixture()
async def engine(request):
    uri = os.environ['MNBOT_DATABASE_URI']
    root_engine = sqlalchemy.ext.asyncio.create_async_engine(uri, isolation_level = "AUTOCOMMIT")
    async with root_engine.connect() as conn:
        await conn.execute(sqlalchemy.text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(sqlalchemy.text("CREATE SCHEMA public AUTHORIZATION pg_database_owner"))
        await conn.execute(sqlalchemy.text("GRANT USAGE ON SCHEMA public TO PUBLIC"))
        await conn.execute(sqlalchemy.text("GRANT ALL ON SCHEMA public TO pg_database_owner"))
        await conn.commit()
    await create(CreateArgs(uri, 2))

    engine = await db.create_engine(uri)
    yield engine
    await engine.dispose()

    await root_engine.dispose()

def _test(f):
    return pytest.mark.asyncio(f)

def default_ruleset():
    return db.JobRuleset(
            db.generate_id(),
            ua = db.RulesetColumn("default", []),
            skip = db.RulesetColumn(False, []),
            accept = db.RulesetColumn(False, []),
            custom_js = db.RulesetColumn(None, [])
    )

def settings(**kwargs):
    kwargs = {"ua": "default", "skip": False, "accept": False, "custom_js": None} | kwargs
    return db.PageSettings(**kwargs)

@_test
async def test_authenticate(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests that pipeline authentication works.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("foo", False, "password")
        await q.create_pipeline("bar", True, "password1")
        foo = await q.pipeline("foo")
        bar = await q.pipeline("bar")

        with pytest.raises(db.AuthenticationFailure):
            await foo.authenticate("password1")
        with pytest.raises(db.AuthenticationFailure):
            await bar.authenticate("password")
        await foo.authenticate("password")
        await bar.authenticate("password1")

@_test
async def test_get_job_counts(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests that the active job count works.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        async def make_job():
            await q.create_jobs([db.JobCreation(db.generate_id(), "foo", {}, "", default_ruleset())])

        assert (await q.get_job_counts()) == 0
        await make_job()
        assert (await q.get_job_counts()) == 1
        await make_job()
        await make_job()
        assert (await q.get_job_counts()) == 3

async def make_job(q, status = model.JobStatus.ACTIVE, concurrency = 0, nice = 0, tag = None, depth = None, initial_ruleset = None):
    if initial_ruleset is None:
        initial_ruleset = default_ruleset()
    id = db.generate_id()
    await q.create_jobs([db.JobCreation(id, "foo", {}, "", initial_ruleset, status, concurrency, nice, tag, None, depth)])
    return id

async def make_pages(q, job_id, *payloads, parent_page = None):
    pagecs = []
    ids = []
    for page in payloads:
        id = db.generate_id()
        ids.append(id)
        pagecs.append(db.PageCreation(id, page, parent_page))
    pages = await q.create_pages(job_id, pagecs)
    return [pages[page_id] for page_id in ids]

async def check_claim(q, expected_id, matchonly = False):
    pipe = await q.pipeline("pipe")
    res = await pipe._find_claimable_job(matchonly, None)
    assert res == expected_id

async def check_all_claims(q, *expected_ids):
    res = await q.get_all_claimable_jobs()
    assert tuple(res) == expected_ids

@_test
async def test_job_order(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests that jobs are dequeued in the correct order.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        await check_claim(q, None)
        # Concurrency 0 or status != active = no claim
        await make_job(q)
        await make_job(q, status = model.JobStatus.DRAINING, concurrency = 1)
        await check_claim(q, None)
        await check_all_claims(q)

        # The pipeline has no tags associated with it, so tags should not work
        idtag = await make_job(q, concurrency = 1, tag = "baz", nice = 999)
        await check_claim(q, None)
        # However, it should show up in check_all_claims, which has no restrictions on tags
        await check_all_claims(q, idtag)

        id0 = await make_job(q, concurrency = 1, nice = 1)
        await check_claim(q, id0)
        id1 = await make_job(q, concurrency = 1)
        await check_claim(q, id1)
        id2 = await make_job(q, concurrency = 7)
        await check_claim(q, id1)
        await check_all_claims(q, id1, id2, id0, idtag)
        id3 = await make_job(q, concurrency = 1, nice = -1)
        await check_all_claims(q, id3, id1, id2, id0, idtag)

@_test
async def test_tags(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests that the tagging system works properly.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", True, "password")
        pipe = await q.pipeline("pipe")
        await pipe.create_tags("foo", "bar")
        # Ensure duplicate tags are silently ignored and don't throw an error
        await pipe.create_tags("foo", "baz")

        # This tag is not assigned to the pipeline, so it shouldn't work
        id1 = await make_job(q, concurrency = 1, tag = "quux")
        await check_claim(q, None)
        await check_claim(q, None, matchonly = True)
        await check_all_claims(q, id1)

        id2 = await make_job(q, concurrency = 1, tag = "foo")
        await check_claim(q, id2)
        await check_claim(q, id2, matchonly = True)
        await check_all_claims(q, id1, id2)

        # This job has no tag, so matchonly should ignore it
        id3 = await make_job(q, concurrency = 1, nice = -1)
        await check_claim(q, id3)
        await check_claim(q, id2, matchonly = True)
        await check_all_claims(q, id3, id1, id2)

        # Remove tag and see if id2 disappears from matchonly
        await pipe.remove_tags("foo")
        await check_claim(q, id3)
        await check_claim(q, None, matchonly = True)
        await check_all_claims(q, id3, id1, id2)

@_test
async def test_claiming(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests set_claim.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        job = await make_job(q, concurrency = 1)
        job2 = await make_job(q, concurrency = 1)
        async def check_claim_count(expected1, expected2):
            q = sqlalchemy.select(model.jobs.c.active_claims).where(model.jobs.c.job_id == job)
            val = (await conn.execute(q)).one()[0]
            q = sqlalchemy.select(model.jobs.c.active_claims).where(model.jobs.c.job_id == job2)
            val2 = (await conn.execute(q)).one()[0]
            assert (val, val2) == (expected1, expected2)

        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0, 1)

        await check_claim_count(0, 0)
        await pipe._set_claim(0, job)
        await check_claim_count(1, 0)
        await pipe._set_claim(0, job2)
        await check_claim_count(0, 1)
        await pipe._set_claim(1, job)
        await check_claim_count(1, 1)
        await pipe._set_claim(0, job)
        await check_claim_count(2, 0)
        await pipe._set_claim(0, None)
        await check_claim_count(1, 0)
        await pipe._set_claim(0, None)
        await check_claim_count(1, 0)
        await pipe._set_claim(1, None)
        await check_claim_count(0, 0)

        with pytest.raises(db.NoSuchPipelineError):
            await pipe._set_claim(2, job)

@_test
async def test_pipeline(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests basic pipeline lifecycle.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        # Create pipelines - one matchonly, one not
        await q.create_pipeline("pipe", False, "password")
        await q.create_pipeline("pipe_mo", True, "password")
        # Register two slots for each
        pipe = await q.pipeline("pipe", 0, 1)
        pipe_mo = await q.pipeline("pipe_mo", 0, 1)
        # Add a tag to both
        await pipe.create_tags("tag")
        await pipe_mo.create_tags("tag")

        # ... Ok, time for some testing!
        job1 = await make_job(q, concurrency = 3)
        pageids = await make_pages(q, job1, "one", "two", "three", "four")
        pages = []
        pages.extend((
            # one
            await pipe.find_claim_page("", 0),
            # two
            await pipe.find_claim_page("", 1),
            # None
            await pipe_mo.find_claim_page("", 1),
        ))
        await conn.execute(sqlalchemy.update(model.jobs).where(model.jobs.c.job_id == job1).values(tag = "tag"))
        pages.extend((
            # three
            await pipe_mo.find_claim_page("", 1),
            # None (reached concurrency limit)
            await pipe_mo.find_claim_page("", 0),
        ))
        print(pages)
        found_pages = [page.page_id if page else None for page in pages]
        found_payloads = [page.payload if page else None for page in pages]
        expected_pages = pageids[0:2] + [None, pageids[2], None]
        expected_payloads = ["one", "two", None, "three", None]
        assert found_pages == expected_pages
        assert found_payloads == expected_payloads

@_test
async def test_empty_create_pages_set(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests that calling create_pages with no arguments does nothing.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        job_id = await make_job(q, concurrency = 1)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)
        assert (await q.create_pages(job_id, [])) == {}
        assert (await q.create_pages(job_id, (i for i in []))) == {}
        await conn.commit()
        with pytest.raises(db.JobExhausted):
            await pipe.find_claim_page("", 0)
        await q.update_job_status(job_id)
        assert (await q.get_job_counts()) == 0

@_test
async def test_retries(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests the retrying/finishing system.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0, 1)
        job_id = await make_job(q, concurrency = 2)
        await make_pages(q, job_id, "one", "two", "three", "four")

        # Claim two pages, finish one of them
        claim1 = await pipe.find_claim_page("", 0)
        assert claim1 and claim1.payload == "one"
        claim2 = await pipe.find_claim_page("", 1)
        assert claim2 and claim2.payload == "two"
        await pipe.finish_attempt(claim1.attempt_id)
        # Claim a third page, fail it non-fatally. It should be returned to the queue
        claim3 = await pipe.find_claim_page("", 0)
        assert claim3 and claim3.payload == "three"
        # When it is returned to the queue it should have one try remaining
        assert (await pipe.fail_attempt(claim3.attempt_id, "error", False)) == 1
        # Claim a fourth page, failing it fatally
        claim4 = await pipe.find_claim_page("", 0)
        assert claim4 and claim4.payload == "four"
        # Should thus have no tries remaining
        assert (await pipe.fail_attempt(claim4.attempt_id, "error", True)) == 0
        # Ensure claim 3 was recycled back into the queue, and that max tries is taken into account.
        claim3_2 = await pipe.find_claim_page("", 0)
        assert claim3_2 and claim3_2.payload == "three"
        assert (await pipe.fail_attempt(claim3_2.attempt_id, "error", False)) == 0
        with pytest.raises(db.JobExhausted):
            await pipe.find_claim_page("", 0)
            # Ordinarily we would now recalculate the job status, but not in this test
        # Ensure that adding retries manually works as intended
        await q.retry_page(claim3_2.page_id, 1)
        claim3_4 = await pipe.find_claim_page("", 0)
        assert claim3_4 and claim3_4.payload == "three"
        assert (await pipe.fail_attempt(claim3_4.attempt_id, "error", False)) == 0
        with pytest.raises(db.JobExhausted):
            await pipe.find_claim_page("", 0)

async def check_job_status(queue: db.Connection, job_id, expected_status):
    q = sqlalchemy.select(model.jobs.c.status).where(model.jobs.c.job_id == job_id)
    res = (await queue.conn.execute(q)).first()
    assert res and res[0] == expected_status

@_test
async def test_finishing(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests the update_job_status function.
    """
    async with engine.connect() as conn:
        queue = db.Connection(conn)
        await queue.create_pipeline("pipe", False, "password")
        pipe = await queue.pipeline("pipe", 0, 1)
        # Create two jobs with some pages
        job1 = await make_job(queue, concurrency = 2)
        await check_job_status(queue, job1, model.JobStatus.ACTIVE)
        job2 = await make_job(queue, concurrency = 1)
        await check_job_status(queue, job2, model.JobStatus.ACTIVE)
        with pytest.raises(AssertionError):
            await check_job_status(queue, job2, model.JobStatus.DRAINING)
        await make_pages(queue, job1, "1one", "1two", "1three")
        await make_pages(queue, job2, "2one")

        # Start claiming from job 1
        claim1_1 = await pipe.find_claim_page("", 0)
        assert claim1_1 and claim1_1.payload == "1one"
        await pipe.finish_attempt(claim1_1.attempt_id)
        assert (await pipe.parent.update_job_status(job1)) == model.JobStatus.ACTIVE
        await check_job_status(queue, job1, model.JobStatus.ACTIVE)
        # Fail page non-fatally
        claim1_2 = await pipe.find_claim_page("", 0)
        assert claim1_2 and claim1_2.payload == "1two"
        await pipe.fail_attempt(claim1_2.attempt_id, "", False)
        assert (await pipe.parent.update_job_status(job1)) == model.JobStatus.ACTIVE
        await check_job_status(queue, job1, model.JobStatus.ACTIVE)
        # Claim third page but don't fail it yet
        claim1_3 = await pipe.find_claim_page("", 0)
        # It should still be active...
        assert (await pipe.parent.update_job_status(job1)) == model.JobStatus.ACTIVE
        assert claim1_3 and claim1_3.payload == "1three"
        # but if we reclaim the failed page, it should be DRAINING
        claim1_2_2 = await pipe.find_claim_page("", 1)
        assert claim1_2_2 and claim1_2_2.payload == "1two"
        assert (await pipe.parent.update_job_status(job1)) == model.JobStatus.DRAINING
        # Same goes for if we finish one of them (but not both)
        await pipe.fail_attempt(claim1_2_2.attempt_id, "", False)
        assert (await pipe.parent.update_job_status(job1)) == model.JobStatus.DRAINING
        await check_job_status(queue, job1, model.JobStatus.DRAINING)
        # And if we finish the other, we're done :-)
        await pipe.finish_attempt(claim1_3.attempt_id)
        assert (await pipe.parent.update_job_status(job1)) == model.JobStatus.DONE
        await check_job_status(queue, job1, model.JobStatus.DONE)

        await check_job_status(queue, job2, model.JobStatus.ACTIVE)
        claim2_1 = await pipe.find_claim_page("", 0)
        assert claim2_1 and claim2_1.payload == "2one"
        await pipe.fail_attempt(claim2_1.attempt_id, "", True)
        assert (await pipe.parent.update_job_status(job2)) == model.JobStatus.DONE
        await check_job_status(queue, job2, model.JobStatus.DONE)

        # Finally, adding more pages should make it ACTIVE again.
        await make_pages(queue, job2, "hi")
        assert (await pipe.parent.update_job_status(job2)) == model.JobStatus.ACTIVE

@_test
async def test_update_job_status_with_abort(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)
        job1 = await make_job(q, concurrency = 1)
        await make_pages(q, job1, "a")

        await q.abort_job(job1)
        assert (await pipe.find_claim_page("", 0)) is None
        assert (await q.update_job_status(job1) == model.JobStatus.ABORTED)
        assert (await q.update_job_status(job1, True) == model.JobStatus.ACTIVE)
        # Claim page, ensure it is be set to DRAINING
        res = await pipe.find_claim_page("", 0)
        assert res and res.payload == "a"
        await q.abort_job(job1)
        assert (await q.update_job_status(job1) == model.JobStatus.ABORTED)
        assert (await q.update_job_status(job1, True) == model.JobStatus.DRAINING)
        await q.abort_job(job1)
        await pipe.finish_attempt(res.attempt_id)
        assert (await q.update_job_status(job1) == model.JobStatus.ABORTED)
        assert (await q.update_job_status(job1, True) == model.JobStatus.DONE)

@_test
async def test_update_job_status_with_depth(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)

        job1 = await make_job(q, concurrency = 1, depth = 0)
        (a,) = await make_pages(q, job1, "a")
        (b,) = await make_pages(q, job1, "b", parent_page = a)
        assert (await q.update_job_status(job1)) == model.JobStatus.ACTIVE
        claim = (await pipe.find_claim_page("", 0))
        assert claim and claim.page_id == a
        assert (await q.update_job_status(job1)) == model.JobStatus.DRAINING
        await pipe.finish_attempt(claim.attempt_id)
        # At this point, there is still an item, but it is out of scope and so the job is done.
        assert (await q.update_job_status(job1)) == model.JobStatus.DONE

@_test
async def test_update_job_status_with_skip(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)

        initial_ruleset = default_ruleset()
        initial_ruleset.skip.add(db.JobRule("", True))
        job1 = await make_job(q, concurrency = 1, initial_ruleset = initial_ruleset)
        await make_pages(q, job1, "foo")
        # update_job_status will not be aware of the SKIP rule
        assert (await q.update_job_status(job1)) == model.JobStatus.ACTIVE
        # find_claim_page will be, though, and the claim will fail
        try:
            await pipe.find_claim_page("", 0)
        except db.JobExhausted as e:
            assert e.job_id == job1
        else:
            raise AssertionError("Exception not raised")
        # Now that the page has been marked SKIPPED, update_job_status should function correctly
        assert (await q.update_job_status(job1)) == model.JobStatus.DONE
        assert (await pipe.find_claim_page("", 0)) is None

async def _get_job_depth(q, job_id, pipe):
    res = await q.conn.execute(sqlalchemy.select(pipe.parent._job_depth(job_id)))
    return res.first()

async def _assert_eligible_jobs(pipe: db.Pipeline, job_id: model.UUID, expected_eligible: list, expected_ineligible: list):
    """
    Asserts that all jobs are considered eligible or ineligible. (The order of dequeuing is not checked.)
    """
    discovered_e = []
    discovered_i = []
    for page in expected_eligible:
        res = await pipe._find_claimable_page(job_id, _page_id = page)
        discovered_e.append(res.page_id if res else f"ineligible[{page}]")
    for page in expected_ineligible:
        res = await pipe._find_claimable_page(job_id, _page_id = page)
        discovered_i.append(page if not res else f"eligible[{page}]")
    assert discovered_e == expected_eligible
    assert discovered_i == expected_ineligible

async def _get_page_depths(pipe, *page_ids):
    depths = []
    for page_id in page_ids:
        res = await pipe.parent.conn.execute(sqlalchemy.select(pipe.parent._page_depth(page_id)))
        depths.append(res.first()[0])
    return depths

@_test
async def test_depth_tracking(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests whether the relations system works as expected.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)

        job1 = await make_job(q, concurrency = 1, depth = None)
        root1, root2 = await make_pages(q, job1, "foo", "bar", parent_page = None)
        page11 = (await make_pages(q, job1, "baz", parent_page = root1))[0]
        page21, page22 = await make_pages(q, job1, "a", "b", parent_page = root2)

        res = await _get_job_depth(q, job1, pipe)
        assert res and res[0] is None
        assert (await _get_page_depths(pipe, root1, root2, page11, page21, page22)) == [0, 0, 1, 1, 1]
        await _assert_eligible_jobs(pipe, job1, [root1, root2, page11, page21, page22], [])
        await conn.execute(sqlalchemy.update(model.jobs).where(model.jobs.c.job_id == job1).values(depth = -1))
        res = await _get_job_depth(q, job1, pipe)
        assert res and res[0] == -1
        await _assert_eligible_jobs(pipe, job1, [], [root1, root2, page11, page21, page22])
        with pytest.raises(AssertionError):
            # Who tests the tests?
            await _assert_eligible_jobs(pipe, job1, [root1], [root2, page11, page21, page22])
        await conn.execute(sqlalchemy.update(model.jobs).where(model.jobs.c.job_id == job1).values(depth = 1))
        await _assert_eligible_jobs(pipe, job1, [root1, root2, page11, page21, page22], [])
        # Add new page of depth 2, which should be ineligible
        (page211,) = await make_pages(q, job1, "c", parent_page = page21)
        await _assert_eligible_jobs(pipe, job1, [root1, root2, page11, page21, page22], [page211])
        assert (await _get_page_depths(pipe, page211)) == [2]
        # Add new path for page211 of depth 1, which should make it eligible
        page23, page24 = await make_pages(q, job1, "c", "d", parent_page = root2)
        assert page23 == page211
        assert (await _get_page_depths(pipe, page211, page24)) == [1, 1]
        await _assert_eligible_jobs(pipe, job1, [root1, root2, page11, page21, page22, page23, page24], [])
        # Add new path of depth 4, which should have no effect
        (page2111,) = await make_pages(q, job1, "c", parent_page = page211)
        assert page2111 == page23
        assert (await _get_page_depths(pipe, page211)) == [1]
        await _assert_eligible_jobs(pipe, job1, [root1, root2, page11, page21, page22, page23, page24], [])
        # Add some cycles to ensure nothing hangs
        (root1a,) = await make_pages(q, job1, "foo", parent_page = root1)
        assert root1a == root1
        assert (await _get_page_depths(pipe, root1)) == [0]
        await _assert_eligible_jobs(pipe, job1, [root1, root2, page11, page21, page22, page23, page24], [])
        (root1b,) = await make_pages(q, job1, "foo", parent_page = page11)
        assert root1b == root1
        assert (await _get_page_depths(pipe, root1)) == [0]
        await _assert_eligible_jobs(pipe, job1, [root1, root2, page11, page21, page22, page23, page24], [])

@_test
async def test_attempt_id_to_job_id(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests whether attempt_id_to_job_id functions as expected.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)

        job1 = await make_job(q, concurrency = 1)
        await make_pages(q, job1, "item1")
        cl = await pipe.find_claim_page("", 0)
        assert cl and (await pipe.parent.attempt_id_to_job_id(cl.attempt_id)) == (job1, tuple())
        res2 = await pipe.parent.attempt_id_to_job_id(cl.attempt_id, [model.jobs.c.depth])
        assert res2 == (job1, (None,))

@_test
async def test_compute_page_settings():
    ruleset = default_ruleset()
    ruleset.accept.rules = [db.JobRule(r"", False), db.JobRule(r"aaa", True)]
    ruleset.skip.rules = [db.JobRule(r"test", True)]
    ruleset.custom_js.rules = [db.JobRule(r"test", "foo"), db.JobRule(r"aaa", None)]
    ruleset.ua.rules = [db.JobRule(r"^https?://hello\d\.very-good-quality-co\.de/", "ua1")]

    tests = (
        ("", settings(accept = False)),
        ("https://example.org", settings(accept = False)),
        ("https://hello4.very-good-quality-co.de/robots.txt", settings(accept = False, ua = "ua1")),
        ("https://hello6.very-good-quality-co.de/test", settings(accept = False, ua = "ua1", skip = True, custom_js = "foo")),
        ("http://example.com/aaa", settings(accept = True)),
        ("http://example.com/aaa/test", settings(accept = True, skip = True)),
    )
    for url, expected in tests:
        result = db.PageSettings.from_ruleset(url, ruleset)
        print(url, expected, result)
        assert result == expected
    with pytest.raises(AssertionError):
        result = db.PageSettings.from_ruleset("", ruleset)
        assert result == settings(accept = True)

@_test
async def test_tag_rowcount(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests the return value in create_tags, remove_tags, and get_tags.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe")
        async def create(expected, *tags):
            # There doesn't currently seem to be an easy way to return this, so don't test it for now
            await pipe.create_tags(*tags)

        async def remove(expected, *tags):
            r = await pipe.remove_tags(*tags)
            assert r == expected

        async def get(*expected):
            r = await pipe.get_tags()
            assert r == set(expected)

        await get()
        await create(2, "foo", "bar")
        await get("foo", "bar")
        await remove(2, "foo", "bar")
        await get()
        await create(2, "foo", "bar", "foo")
        await remove(1, "foo", "foo", "baz")
        await get("bar")
        await create(2, "foo", "bar", "baz")
        await get("foo", "bar", "baz")
        await remove(3, "foo", "bar", "baz")
        await get()

@_test
async def test_new_job_ruleset(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        job1 = await make_job(q)

        tmp_ruleset = default_ruleset()
        tmp_ruleset.accept.default = True

        rs1 = await q.get_job_ruleset(job1)
        tmp_ruleset.job_ruleset_id = rs1.job_ruleset_id
        assert tmp_ruleset != rs1
        with pytest.raises(db.RulesetConflict):
            await q.new_ruleset(job1, tmp_ruleset, db.generate_id())
        assert (await q.get_job_ruleset(job1)) == rs1
        tmp_ruleset.job_ruleset_id = await q.new_ruleset(job1, tmp_ruleset, rs1.job_ruleset_id)
        assert (await q.get_job_ruleset(job1)) == tmp_ruleset

@_test
async def test_job_rule_insertion(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests create_job_rule and remove_job_rule.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)

        # Create our job rules
        initial_ruleset = default_ruleset()
        initial_ruleset.ua.default = "quux"
        initial_ruleset.ua.add(db.JobRule("^https?://", "minimal"))

        job1 = await make_job(q, initial_ruleset = initial_ruleset)

        ruleset = await q.get_job_ruleset(job1)
        ruleset.ua.add(db.JobRule("^https?://example.org$", "stealth"))
        cjs_1 = ruleset.custom_js.add(db.JobRule("^https?://", "foo"))
        ruleset.custom_js.add(db.JobRule("https?://", "bar"), cjs_1)
        skip_1 = ruleset.skip.add(db.JobRule("", True))
        new_id = await q.new_ruleset(job1, ruleset)
        from_db = await q.get_job_ruleset(job1)
        ruleset.job_ruleset_id = new_id
        assert ruleset == from_db
        assert [rule.scope for rule in ruleset.ua.rules] == ["^https?://", "^https?://example.org$"]
        assert [rule.scope for rule in ruleset.custom_js.rules] == ["https?://", "^https?://"]
        assert [rule.scope for rule in ruleset.skip.rules] == [""]
        assert ruleset.accept.rules == []
        assert ruleset.ua.default == "quux"

        # Create some pages. No trailing slash, so r2 applies and ua will be stealth
        await make_pages(q, job1, "http://example.org", "https://example.org")
        # Ensure page settings are computed correctly
        assert db.PageSettings.from_ruleset("http://example.org", ruleset) == db.PageSettings(ua = "stealth", custom_js = "foo", skip = True, accept = initial_ruleset.accept.default)
        # Skip rule is in place, so no item should be dequeued
        assert (await pipe._find_claimable_page(job1)) is None

        # No more skip rule...
        ruleset.skip.remove(skip_1)
        ruleset.job_ruleset_id = await q.new_ruleset(job1, ruleset)
        # Ensure the correct one (i.e. the empty regex) was removed, and that computed page settings change accordingly
        assert [rule.scope for rule in ruleset.ua.rules] == ["^https?://", "^https?://example.org$"]
        assert [rule.scope for rule in ruleset.custom_js.rules] == ["https?://", "^https?://"]
        assert [rule.scope for rule in ruleset.skip.rules] == []
        assert db.PageSettings.from_ruleset("http://example.org", ruleset) == db.PageSettings(ua = "stealth", custom_js = "foo", skip = False, accept = initial_ruleset.accept.default)
        # Queue some more pages, now with trailing slash (to prevent unique conflict)
        (page1, page2) = await make_pages(q, job1, "https://example.org/", "http://example.org/")
        info = await pipe._find_claimable_page(job1)
        assert info == db.PendingPage(page1, "https://example.org/", ruleset.job_ruleset_id, db.PageSettings(ua = "minimal", custom_js = "foo", skip = initial_ruleset.skip.default, accept = initial_ruleset.accept.default))

        oldval = ruleset.ua.remove(0)
        assert oldval.scope == "^https?://"
        with pytest.raises(IndexError):
            ruleset.ua.remove(1)
        await q.new_ruleset(job1, ruleset)
        assert [rule.scope for rule in (await q.get_job_ruleset(job1)).ua.rules] == ["^https?://example.org$"]
        oldval = ruleset.ua.remove(0)
        assert oldval.scope == "^https?://example.org$"
        assert ruleset.ua.rules == []

        ruleset.custom_js.remove(0)
        ruleset.custom_js.remove(0)
        assert ruleset.skip.rules + ruleset.accept.rules + ruleset.custom_js.rules + ruleset.ua.rules == []
        ruleset.job_ruleset_id = await q.new_ruleset(job1, ruleset)

        assert db.PageSettings.from_ruleset("", ruleset).ua == "quux"

        with pytest.raises(IndexError):
            ruleset.ua.add(db.JobRule("", "hi"), 1000)
        with pytest.raises(IndexError):
            ruleset.ua.remove(1000)
        assert (await q.get_job_ruleset(job1)) == ruleset

# Test ensure_ruleset
# Ensure conflicts don't change the ID

@_test
async def test_job_rule_removal_by_scope():
    ruleset = default_ruleset()
    assert ruleset.ua.add(db.JobRule("foo", "")) == 0
    assert ruleset.ua.add(db.JobRule("bar", "")) == 1
    assert ruleset.ua.add(db.JobRule("foo", "")) == 2
    assert ruleset.ua.add(db.JobRule("foo", "")) == 3
    assert ruleset.skip.add(db.JobRule("foo", False)) == 0

    assert ruleset.ua.rules == [db.JobRule("foo", ""), db.JobRule("bar", ""), db.JobRule("foo", ""), db.JobRule("foo", "")]
    assert ruleset.skip.rules == [db.JobRule("foo", False)]

    assert ruleset.ua.remove_by_scope("foo") == 3
    assert ruleset.ua.rules == [db.JobRule("bar", "")]
    assert ruleset.skip.rules == [db.JobRule("foo", False)]
    assert ruleset.ua.remove_by_scope("foo") == 0
    assert ruleset.ua.rules == [db.JobRule("bar", "")]
    assert ruleset.skip.rules == [db.JobRule("foo", False)]

    assert ruleset.ua.remove_by_scope("bar") == 1
    assert ruleset.ua.remove_by_scope("bar") == 0
    assert ruleset.ua.rules == []
    assert ruleset.skip.rules == [db.JobRule("foo", False)]

@_test
async def test_job_rule_removal_by_scope_all():
    ruleset = default_ruleset()
    assert ruleset.ua.add(db.JobRule("foo", "")) == 0
    assert ruleset.ua.add(db.JobRule("bar", "")) == 1
    assert ruleset.ua.add(db.JobRule("foo", "")) == 2
    assert ruleset.ua.add(db.JobRule("foo", "")) == 3
    assert ruleset.skip.add(db.JobRule("foo", False)) == 0

    assert ruleset.ua.rules == [db.JobRule("foo", ""), db.JobRule("bar", ""), db.JobRule("foo", ""), db.JobRule("foo", "")]
    assert ruleset.skip.rules == [db.JobRule("foo", False)]

    assert ruleset.remove_all_by_scope("foo") == 4
    assert ruleset.ua.rules == [db.JobRule("bar", "")]
    assert ruleset.skip.rules == []

@_test
async def test_job_rule_edge_cases():
    """
    Tests some possible edge cases related to job rulesets.
    """
    ruleset = default_ruleset()

    # This ruleset is empty! Deletion shouldn't work.
    with pytest.raises(IndexError):
        ruleset.accept.remove(0)

    # Negative indexing probably shouldn't be allowed.
    with pytest.raises(IndexError):
        ruleset.skip.add(db.JobRule("", False), -1)
    # Inserting at len(rules) to append is not supported.
    with pytest.raises(IndexError):
        ruleset.custom_js.add(db.JobRule("", None), 0)
    # And check negative indexing when there *is* a rule, too.
    ruleset.custom_js.add(db.JobRule("", None))
    with pytest.raises(IndexError):
        ruleset.custom_js.add(db.JobRule("", None), -1)
    with pytest.raises(IndexError):
        ruleset.custom_js.remove(-1)
    # Removing len(rules) should definitely not work.
    with pytest.raises(IndexError):
        ruleset.custom_js.remove(1)

@_test
async def test_ruleset_removal_order(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    """
    Tests that removing rules preserves the order of the other ones.
    Kind of tested above, but this is a little more explicit.
    """
    async with engine.connect() as conn:
        q = db.Connection(conn)
        job1 = await make_job(q, concurrency = 1)
        ruleset = await q.get_job_ruleset(job1)

        for scope in ("foo", "removeme", "bar", "removeme", "baz", "quux"):
            ruleset.ua.add(db.JobRule(scope, "default"))
        ruleset.skip.add(db.JobRule("apple", True))
        new_id = await q.new_ruleset(job1, ruleset)
        ruleset.job_ruleset_id = new_id
        assert ruleset == await q.get_job_ruleset(job1)
        ruleset.ua.remove(0)
        ruleset.ua.remove(4)
        assert [rule.scope for rule in ruleset.ua.rules] == ["removeme", "bar", "removeme", "baz"]
        assert [rule.scope for rule in ruleset.skip.rules] == ["apple"]
        assert [rule.scope for rule in ruleset.accept.rules] == []
        assert [rule.scope for rule in ruleset.custom_js.rules] == []
        ruleset.ua.remove_by_scope("removeme")
        assert [rule.scope for rule in ruleset.ua.rules] == ["bar", "baz"]
        assert [rule.scope for rule in ruleset.skip.rules] == ["apple"]
        ruleset.skip.remove_by_scope("apple")
        assert [rule.scope for rule in ruleset.ua.rules] == ["bar", "baz"]
        assert [rule.scope for rule in ruleset.skip.rules] == []

@_test
async def test_many_skipped_jobs(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        job1 = await make_job(q, concurrency = 1)
        ruleset = await q.get_job_ruleset(job1)
        ruleset.skip.add(db.JobRule("^skipped_", True))
        await q.new_ruleset(job1, ruleset)
        await make_pages(q, job1, *[f"skipped_{i}" for i in range(100)], "hello")
        ruleset.ua.add(db.JobRule("", "stealth"))
        await q.new_ruleset(job1, ruleset)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)

        claim = await pipe.find_claim_page("", 0)
        assert claim
        assert claim.payload == "hello"
        assert claim.settings == db.PageSettings(ua = "stealth", accept = False, skip = False, custom_js = None)
        with pytest.raises(db.JobExhausted):
            await pipe.find_claim_page("", 0)

gb = lambda b : b * 1024 * 1024 * 1024

@_test
async def test_pipeline_heartbeat(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe")

        assert await pipe.get_last_heartbeat() is None
        await pipe.heartbeat(gb(1), gb(20))
        res = await pipe.get_last_heartbeat()
        assert res and res == db.PipelineHealth(gb(1), gb(20), res.last_checkin)
        assert await q.get_pipeline_health_status(gb(0.5), datetime.timedelta(seconds = 1)) == db.PipelineHealthStatus.HEALTHY
        await asyncio.sleep(1)
        assert await q.get_pipeline_health_status(gb(0.5), datetime.timedelta(seconds = 1)) == db.PipelineHealthStatus.UNHEALTHY

        await q.create_pipeline("pipe2", False, "password")
        pipe2 = await q.pipeline("pipe2")
        assert await q.get_pipeline_health_status(gb(0.5), datetime.timedelta(seconds = 1)) == db.PipelineHealthStatus.UNHEALTHY
        await pipe2.heartbeat(gb(0.5), gb(20))
        assert await q.get_pipeline_health_status(gb(0.5), datetime.timedelta(seconds = 1)) == db.PipelineHealthStatus.DEGRADED
        await pipe2.heartbeat(gb(0), gb(20))
        assert await q.get_pipeline_health_status(gb(0.5), datetime.timedelta(seconds = 1)) == db.PipelineHealthStatus.UNHEALTHY
        await pipe2.heartbeat(gb(20), gb(20))
        assert await q.get_pipeline_health_status(gb(0.5), datetime.timedelta(seconds = 1)) == db.PipelineHealthStatus.DEGRADED
        assert await q.get_pipeline_health_status(gb(0), datetime.timedelta(seconds = 60)) == db.PipelineHealthStatus.HEALTHY

@_test
async def test_multiple_create_page_same_payload(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        job_id = await make_job(q)
        pages = [db.PageCreation(db.generate_id(), i, None) for i in ("d", "a", "b", "a", "c")]
        await q.create_pages(job_id, pages)
        all_pages = [page.payload async for page in q.all_pending_pages(job_id)]
        assert all_pages == ["d", "a", "b", "c"]

@_test
async def test_result_dupe_ignore(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
        q = db.Connection(conn)
        job_id = await make_job(q, concurrency = 1)
        page_id, = await make_pages(q, job_id, "a")
        await q.create_pipeline("pipe", False, "password")
        pipe = await q.pipeline("pipe", 0)
        claim = await pipe.find_claim_page("", 0)
        assert claim and claim.page_id == page_id
        result_id = db.generate_id()
        await pipe.create_result(claim.attempt_id, result_id, model.ResultType.CUSTOM_JS, {})
        # Second time should also succeed (failing silently as the result already exists)
        await pipe.create_result(claim.attempt_id, result_id, model.ResultType.CUSTOM_JS, {})

@db._wrap_serialization_failure
async def _commit(conn):
    await conn.commit()

@db._wrap_serialization_failure
async def _execute(conn, q):
    await conn.execute(q)

@_test
async def test_serializable_wrapper(engine: sqlalchemy.ext.asyncio.AsyncEngine):
    async with engine.connect() as conn:
            await conn.execute(sqlalchemy.insert(model.options).values(key = "hi", value = "bye"))
            await conn.execute(sqlalchemy.insert(model.options).values(key = "bye", value = "hi"))
            await conn.commit()
    async with engine.connect() as conn1:
        async with engine.connect() as conn2:
            q1 = sqlalchemy.select(model.options.c.value).where(model.options.c.key == "hi")
            q2 = sqlalchemy.update(model.options).where(model.options.c.value == "bye").values(value = "cye")
            q3 = sqlalchemy.update(model.options).where(model.options.c.value == "hi").values(value = "dye")
            await conn1.execute(q1)
            await conn2.execute(q2)
            await conn1.execute(q3)
            await conn2.commit()
            with pytest.raises(db.SerializationFailure):
                await _commit(conn1)
            await conn1.rollback()
            with pytest.raises(sqlalchemy.exc.IntegrityError):
                q1 = sqlalchemy.insert(model.options).values(key = "hi", value = "eye")
                await _execute(conn1, q1)

# create_page: Test the niceness update. (Not currently used, so I'm not that worried right now.)
# TODO: Test more nonexistence errors.
