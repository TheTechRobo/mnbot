"""
Various database functions, put here for ease of reuse (and testing).

The functions in this file that take a connection parameter do not automatically commit.
Transaction management is left to the caller.
"""

import dataclasses
import typing
import os
import regex
import logging

import uuid_utils.compat as uuid
import uuid_utils
import argon2
hasher = argon2.PasswordHasher(time_cost = 2, memory_cost = 47104, parallelism = 1) # slightly higher than OWASP cheat sheet

import sqlalchemy, sqlalchemy.ext.asyncio, sqlalchemy.exc, sqlalchemy.dialects.postgresql
from sqlalchemy.ext.asyncio import AsyncConnection

import asyncpg

from . import model

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

class InvalidQueue(Exception): pass

class NoSuchThingError(Exception): pass
class InvalidIdError(NoSuchThingError): pass
class NoSuchPipelineError(NoSuchThingError): pass

class AlreadyExistsError(Exception): pass
class AuthenticationFailure(Exception): pass
class RulesetConflict(Exception): pass

class JobExhausted(Exception):
    """
    A job was claimed, but no page could be found. The status should be rechecked.
    """
    job_id: model.UUID
    def __init__(self, job_id: model.UUID):
        self.job_id = job_id

class SerializationFailure(Exception):
    """
    The database returned a serialization failure and the transaction must be rolled back.
    The operation may succeed if retried.
    """

def _wrap_serialization_failure(f):
    """
    Decorator that converts serialization exceptions to SerializationFailure.
    """
    async def newf(*args, **kwargs):
        try:
            logger.info(f"enter %s", f.__name__)
            return await f(*args, **kwargs)
        except sqlalchemy.exc.DBAPIError as e:
            if e.orig and isinstance(e.orig.__cause__, asyncpg.exceptions.SerializationError):
                raise SerializationFailure()
            raise
        finally:
            logger.info(f"exit %s", f.__name__)
    return newf

@_wrap_serialization_failure
async def _execute(conn, query):
    return await conn.execute(query)

async def create_engine(uri: str | None = None, check_version = True) -> sqlalchemy.ext.asyncio.AsyncEngine:
    """
    Creates a handle to the database.

    If uri is None, attempts to read the environment variable MNBOT_DATABASE_URI.
    check_version should only be set to False in database creation or migration.
    """
    if uri is None:
        uri = os.environ['MNBOT_DATABASE_URI']

    engine = sqlalchemy.ext.asyncio.create_async_engine(uri, isolation_level = "SERIALIZABLE")
    async with engine.connect() as conn:
        # check_version is never set to false, so this is just a cheeky if + while True
        query = sqlalchemy.select(model.options.c.value).where(model.options.c.key == "version")
        while check_version:
            try:
                result = (await _execute(conn, query)).first()
                if not result:
                    raise InvalidQueue("No version key")
                assert result[0] == str(model.SCHEMA_VERSION)
            except sqlalchemy.exc.ProgrammingError as e:
                raise InvalidQueue(e.orig)
            except SerializationFailure:
                # Try again on serialization failure
                continue
            else:
                break
    return engine

def generate_id() -> model.UUID:
    return uuid.uuid7()

def parse_id(id: str) -> model.UUID:
    try:
        return model.UUID(id)
    except ValueError:
        raise InvalidIdError()

def parse_id_ex(id: str | model.UUID) -> uuid_utils.UUID:
    if isinstance(id, model.UUID):
        id = str(id)
    try:
        return uuid_utils.UUID(id)
    except ValueError:
        raise InvalidIdError()

def _debug_compile_statement(stmt, engine):
    return stmt.compile(dialect = engine.dialect)

@dataclasses.dataclass
class JobCreation:
    job_id: model.UUID
    created_by: str
    metadata: dict
    initial_page: str
    initial_ruleset: "JobRuleset"

    status: model.JobStatus = model.JobStatus.ACTIVE
    concurrency: int = 0
    nice: int = 0
    tag: str | None = None
    note: str | None = None
    depth: int | None = None

@dataclasses.dataclass
class PageCreation:
    page_id: model.UUID
    payload: str
    parent_page: model.UUID | None

    nice: int = 0
    status: model.PageStatus = model.PageStatus.READY

@dataclasses.dataclass
class Counts:
    todo_jobs: int
    fully_claimed_jobs: int
    todo_pages: int
    claimed_pages: int

@dataclasses.dataclass
class PipelineInfo:
    matchonly: bool
    current_claim: tuple[model.UUID | None, model.ClaimLock | None]

@dataclasses.dataclass
class PendingPage:
    page_id: model.UUID
    payload: str
    ruleset_id: model.UUID
    page_settings: "PageSettings"

def _find_claimable_job_q(pipeline: str | None, matchonly: bool, include_existing: model.UUID | None, limit: bool):
    """
    Creates a query for find_claimable_job.
    If pipeline is null, remove all filters on tag. This is useful when displaying the queue.
    """
    if pipeline is not None:
        tag_criteria = sqlalchemy.exists().where((model.tags.c.pipeline_id == pipeline) & (model.jobs.c.tag == model.tags.c.tag))
        if matchonly:
            tag_where_clause = (model.jobs.c.tag != None) & tag_criteria
        else:
            tag_where_clause = (model.jobs.c.tag == None) | tag_criteria
    else:
        tag_where_clause = sqlalchemy.true()

    concurrency_where = model.jobs.c.concurrency > model.jobs.c.active_claims
    if include_existing:
        concurrency_where |= ((model.jobs.c.job_id == include_existing) & (model.jobs.c.concurrency == model.jobs.c.active_claims))

    q = (
        sqlalchemy.select(model.jobs.c.job_id)
        .where( model.jobs.c.status == model.JobStatus.ACTIVE)
        .where(concurrency_where)
        .where(tag_where_clause)
        .order_by(*model.jobs_dequeue_order)
        #.with_for_update(key_share = True)
    )
    if limit:
        q = q.limit(1)
    return q

@dataclasses.dataclass
class JobRule[T]:
    scope: str
    payload: T

    def __post_init__(self):
        self._compiled_scope = regex.compile(self.scope)

    @property
    def compiled_scope(self):
        return self._compiled_scope

    def for_db(self):
        return (self.scope, self.payload)

RulesetPayload = typing.TypeVar("RulesetPayload")

@dataclasses.dataclass
class RulesetColumn[RulesetPayload]:
    default: RulesetPayload
    rules: list[JobRule[RulesetPayload]]

@dataclasses.dataclass
class JobRuleset:
    job_ruleset_id: model.UUID
    ua: RulesetColumn[str]
    custom_js: RulesetColumn[str | None]
    skip: RulesetColumn[bool]
    accept: RulesetColumn[bool]

    @staticmethod
    def _make_column(value):
        return RulesetColumn(value[0], [JobRule(scope, payload) for scope, payload in value[1]])

    @staticmethod
    def _encode_column(column: RulesetColumn):
        return (column.default, [rule.for_db() for rule in column.rules])

    @classmethod
    def from_row(cls, row):
        return cls(
            job_ruleset_id = row.job_ruleset_id,
            ua = cls._make_column(row.ua),
            custom_js = cls._make_column(row.custom_js),
            skip = cls._make_column(row.skip),
            accept = cls._make_column(row.accept),
        )

    def for_db(self, job_id: model.UUID | None = None):
        aux = {"job_id": job_id} if job_id else {}
        return {
                "job_ruleset_id": self.job_ruleset_id,
                "ua": self._encode_column(self.ua),
                "custom_js": self._encode_column(self.custom_js),
                "skip": self._encode_column(self.skip),
                "accept": self._encode_column(self.accept),
        } | aux

@dataclasses.dataclass
class PageSettings:
    ua: str
    custom_js: str | None
    skip: bool
    accept: bool

    @classmethod
    def from_ruleset(cls, url: str, ruleset: JobRuleset) -> typing.Self:
        kwargs = {}
        for key, column in (("ua", ruleset.ua), ("custom_js", ruleset.custom_js), ("skip", ruleset.skip), ("accept", ruleset.accept)):
            kwargs[key] = column.default
            for rule in column.rules:
                if rule.compiled_scope.search(url, timeout = 15):
                    kwargs[key] = rule.payload
        return cls(**kwargs)

    def as_dict(self):
        return dataclasses.asdict(self)

@dataclasses.dataclass
class PageClaimInfo:
    page_id: model.UUID
    job_id: model.UUID
    attempt_id: model.UUID
    payload: str
    settings: PageSettings
    ruleset_id: model.UUID

    def as_json_friendly_dict(self):
        return {
            "page_id": str(self.page_id),
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "payload": self.payload,
            "settings": dataclasses.asdict(self.settings),
            "ruleset_id": str(self.ruleset_id),
        }

class Connection:
    """
    A wrapper around a database connection, with utility methods.

    The AsyncConnection can freely be used on its own alongside this object.
    Note: The connection is not designed to survive an error that is not
    mentioned in the docstring. If a method raises an undocumented exception,
    the transaction should be rolled back.
    """
    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    def id(self):
        """
        Generates a UUIDv7 for use as a primary key.
        """
        return generate_id()

    @_wrap_serialization_failure
    async def create_jobs(self, jobs: typing.Iterable[JobCreation]):
        """
        Adds jobs to the database.
        """
        values = []
        rulesets = []
        for job in jobs:
            values.append(dict(
                job_id = job.job_id,
                status = job.status,
                concurrency = job.concurrency,
                nice = job.nice,
                tag = job.tag,
                created_by = job.created_by,
                note = job.note,
                initial_page = job.initial_page,
                metadata = job.metadata,
                depth = job.depth,
            ))
            rulesets.append(job.initial_ruleset.for_db() | {"job_id": job.job_id})
        await self.conn.execute(sqlalchemy.insert(model.jobs), values)
        await self.conn.execute(sqlalchemy.insert(model.job_rulesets), rulesets)

    @_wrap_serialization_failure
    async def create_pages(self, job_id: model.UUID, pages: typing.Iterable[PageCreation]) -> dict[model.UUID, model.UUID]:
        """
        Adds pages to the database for a particular job.
        Existing pages will be ignored, but the niceness value may be overwritten if the new one is lower.

        Returns a mapping of IDs passed to IDs actually inserted (or existing IDs).

        Important note: If a page with the same payload is passed twice, the niceness from the first entry will always be used.
        Currently, with how this method is used, this is not an issue (every page has the same niceness). But that may change.
        """
        values = []
        relation_values = {}
        # Because payloads are unique, we RETURN the payload to associate it with the caller-given ID.
        # Sentinel columns or postgres' future feature of EXCLUDED clause in RETURNING won't work
        # because we want to include deduplicated rows in the return value.
        payload_to_id_mapping: dict[str, list[model.UUID]] = {}
        num_retries = sqlalchemy.cast(
            sqlalchemy.select(model.options.c.value).where(model.options.c.key == "tries").scalar_subquery(),
            sqlalchemy.SmallInteger
        )
        q = sqlalchemy.dialects.postgresql.insert(model.pages).values(attempts_remaining = num_retries)
        q = q.on_conflict_do_update(constraint = model.pages_index_unique, set_ = dict(
            nice = sqlalchemy.func.least(q.excluded.nice, model.pages.c.nice),
        ))
        q = q.returning(sqlalchemy.text("old.page_id"), sqlalchemy.text("page_id"), model.pages.c.payload)
        for page in pages:
            if page.payload not in payload_to_id_mapping:
                payload_to_id_mapping[page.payload] = []
                # Only insert it if it's never been seen before. (Otherwise, postgres gets sad.)
                values.append(dict(
                    page_id = page.page_id,
                    job_id = job_id,
                    payload = page.payload,
                    # TODO: Use the lowest one of these, if multiple are supplied to the function.
                    nice = page.nice,
                    status = page.status,
                ))
            payload_to_id_mapping[page.payload].append(page.page_id)
            relation_values[page.page_id] = dict(
                page_id = page.page_id,
                job_id = job_id,
                parent_page = page.parent_page,
                # Depth is overwritten by the INSERT trigger.
                depth = -1,
            )
        res = await self.conn.execute(q, values)
        # In case there are existing items, give the actual page IDs to the caller
        id_mapping: dict[model.UUID, model.UUID] = {}
        for existing_id, new_id, payload in res.all():
            # If Postgres gave an existing id, use that. Otherwise, use the id we gave.
            existing_id = existing_id or new_id
            caller_given_ids = payload_to_id_mapping[payload]
            # If the caller passes multiple page objects with the same id, handle them all
            for caller_given_id in caller_given_ids:
                # Associate the caller-given ID in the return value with the existing ID
                id_mapping[caller_given_id] = existing_id
                # Update the relation for that id with the real id
                relation_values[caller_given_id]['page_id'] = existing_id

        # Add relations
        relation_q = sqlalchemy.insert(model.relations)
        await self.conn.execute(relation_q, list(relation_values.values()))
        return id_mapping

    @_wrap_serialization_failure
    async def get_job_counts(self) -> int:
        """
        Gets the number of active/draining jobs.
        """
        q = (
            sqlalchemy.select(sqlalchemy.func.count())
            .select_from(model.jobs)
            .where( model.jobs.c.status.in_((model.JobStatus.ACTIVE, model.JobStatus.DRAINING)))
        )
        cursor = await self.conn.execute(q)
        return cursor.scalar_one()

    @_wrap_serialization_failure
    async def get_all_claimable_jobs(self) -> typing.Sequence[model.UUID]:
        """
        Gets all claimable jobs in order, with no tag restrictions.
        """
        q = _find_claimable_job_q(None, False, None, False)
        cursor = await self.conn.execute(q)
        return [row[0] for row in cursor]

    async def _prepare_slot(self, pipeline: str, *slots):
        """
        Prepares the claim entry for the slots given.
        """
        q = sqlalchemy.dialects.postgresql.insert(model.claims).on_conflict_do_nothing()
        values = []
        for slot in slots:
            values.append(dict(
                pipeline_id = pipeline,
                slot = slot,
                job_id = None,
                lock = None,
            ))
        await self.conn.execute(q, values)

    async def _ensure_pipeline(self, pipeline_id: str):
        """
        Ensures that a pipeline exists.
        """
        q = sqlalchemy.select(model.pipelines.c.pipeline_id).where(model.pipelines.c.pipeline_id == pipeline_id)
        res = await self.conn.execute(q)
        if not res.first():
            raise NoSuchPipelineError()

    @_wrap_serialization_failure
    async def pipeline(self, pipeline_id: str, *slots_to_prepare):
        """
        Returns a Pipeline object with the current connection, and prepares the given slots (if any).
        """
        if slots_to_prepare:
            await self._prepare_slot(pipeline_id, *slots_to_prepare)
        else:
            await self._ensure_pipeline(pipeline_id, *slots_to_prepare)
        return Pipeline(self, pipeline_id)

    @_wrap_serialization_failure
    async def create_pipeline(self, pipeline: str, matchonly: bool, password: str):
        """
        Creates a pipeline.
        """
        hash = hasher.hash(password)
        q = model.pipelines.insert()
        value = dict(
            pipeline_id = pipeline,
            pipeline_secret = hash,
            matchonly = matchonly,
        )
        await self.conn.execute(q, [value])
        return Pipeline(self, pipeline)

    @_wrap_serialization_failure
    async def retry_page(self, page_id: model.UUID, max_tries: int):
        """
        Sets a page's max tries to max_tries, and resets the attempt counter to zero.
        (Existing attempt rows are not changed, only the attempts column.)
        """
        q = (
            sqlalchemy.update(model.pages)
            .where(model.pages.c.page_id == page_id)
            .values(attempts_remaining = max_tries, attempts = 0)
        )
        res = await self.conn.execute(q)
        if res.rowcount == 0:
            raise NoSuchThingError

    @_wrap_serialization_failure
    async def update_job_status(self, job_id: model.UUID, allow_resumption: bool = False) -> model.JobStatus:
        """
        Updates the job status according to the pages left in the queue.
        If allow_resumption is False and the job status is ABORTED, the job will remain ABORTED.

        Note: This method may mark a job as ACTIVE when it really should be marked as DONE,
        as for performance reasons it does not take SKIP rules into account.
        However, the status will be fixed as soon as a pipeline attempts to claim an item.

        Returns the new status.
        """
        # Absolute chonker of a query
        q = (
            sqlalchemy.update(model.jobs)
            .values(
                status = sqlalchemy.case(
                    # If the job is aborted _and_ allow_resumption is False, do nothing.
                    (
                        (model.jobs.c.status == model.JobStatus.ABORTED) & (not allow_resumption),
                        model.jobs.c.status
                    ),
                    # If the job has no available pages...
                    (
                        ~sqlalchemy.exists(self._all_pending_pages_q(job_id)),
                        sqlalchemy.case(
                            # ... set status to DRAINING or DONE based on the number of claimed pages.
                            (
                                sqlalchemy.exists(
                                    sqlalchemy.select(model.pages)
                                    .where(model.pages.c.job_id == model.jobs.c.job_id)
                                    .where(model.pages.c.status == model.PageStatus.CLAIMED)
                                ),
                                sqlalchemy.text("'DRAINING'::jobstatus"),
                            ),
                            else_ = sqlalchemy.text("'DONE'::jobstatus")
                        )
                    ),
                    # Otherwise, the job is active.
                    else_ = sqlalchemy.text("'ACTIVE'::jobstatus"),
                )
            )
            .where(model.jobs.c.job_id == job_id)
            .returning(model.jobs.c.status)
        )
        res = await self.conn.execute(q)
        row = res.first()
        if not row:
            raise NoSuchThingError

        new_status = row[0]
        if new_status in (model.JobStatus.DONE, model.JobStatus.ABORTED):
            # Disclaim this job from all pipelines
            q = (
                sqlalchemy.update(model.claims)
                .where(model.claims.c.job_id == job_id)
                .where(
                    (model.claims.c.lock == None) | (model.claims.c.lock == model.ClaimLock.UNTIL_FINISHED)
                )
                .values(job_id = None, lock = None)
            )
            await self.conn.execute(q)

        return new_status

    @_wrap_serialization_failure
    async def abort_job(self, job_id: model.UUID):
        """
        Sets the status of the given job to ABORTED.
        """
        q = sqlalchemy.update(model.jobs).where(model.jobs.c.job_id == job_id).values(status = model.JobStatus.ABORTED)
        res = await self.conn.execute(q)
        if not res.rowcount:
            raise NoSuchThingError(job_id)

    @_wrap_serialization_failure
    async def is_single_job(self, job_id: model.UUID) -> bool:
        """
        Returns True if the job has exactly one page, and False otherwise.
        """
        q = (
            sqlalchemy.select(sqlalchemy.func.count())
            .select_from(model.pages)
            .where(model.pages.c.job_id == job_id)
            .limit(2)
        )
        res = await self.conn.execute(q)
        row = res.first()
        assert row
        return row[0] == 1

    @_wrap_serialization_failure
    async def attempt_id_to_job_id(self, attempt_id: model.UUID, additional_fields: list | None = None, lock_job: bool = True) -> tuple[model.UUID, typing.Iterable[typing.Any]]:
        """
        Returns the job ID for a given attempt ID, optionally returning additional columns.
        If lock_job is True, a FOR UPDATE lock will be taken on the job row.
        """
        if not additional_fields:
            additional_fields = []
        q = (
            sqlalchemy.select(model.jobs.c.job_id, *additional_fields)
            .where(model.attempts.c.attempt_id == attempt_id)
            .join(model.pages, model.pages.c.page_id == model.attempts.c.page_id)
            .join(model.jobs, model.jobs.c.job_id == model.pages.c.job_id)
        )
        if lock_job:
            q = q.with_for_update(key_share = True, of = model.jobs)
        res = await self.conn.execute(q)
        row = res.first()
        if not row:
            raise NoSuchThingError
        return row[0], row[1:]

    @_wrap_serialization_failure
    async def get_ruleset(self, job_ruleset_id: model.UUID) -> JobRuleset:
        q = (
            sqlalchemy.select(model.job_rulesets)
            .where(model.job_rulesets.c.job_ruleset_id == job_ruleset_id)
        )
        res = await self.conn.execute(q)
        row = res.one()
        if row is None:
            raise NoSuchThingError(job_ruleset_id)
        return JobRuleset.from_row(row)

    @_wrap_serialization_failure
    async def get_job_ruleset(self, job_id: model.UUID, for_update = False) -> JobRuleset:
        q = (
            sqlalchemy.select(model.job_rulesets)
            .where(model.job_rulesets.c.job_id == job_id)
            .order_by(model.job_rulesets.c.job_ruleset_id.desc())
            .limit(1)
        )
        if for_update:
            q = q.with_for_update(key_share = True)
        res = await self.conn.execute(q)
        row = res.one()
        rules = JobRuleset.from_row(row)
        return rules

    @_wrap_serialization_failure
    async def new_ruleset(self, job_id: model.UUID, ruleset: JobRuleset):
        """
        Sets a job's ruleset, overriding previous ones.
        Throws RulesetConflict if a newer ruleset ID exists, which could be caused by a bad system clock.
        """
        # Ensure newer ruleset ID does not exist
        # Serializable isolation should prevent any race condition here
        q = (
            sqlalchemy.select(model.job_rulesets.c.job_ruleset_id)
            .where(model.job_rulesets.c.job_id == job_id)
            .where(model.job_rulesets.c.job_ruleset_id >= ruleset.job_ruleset_id)
            .limit(1)
        )
        row = (await self.conn.execute(q)).one_or_none()
        if row:
            raise RulesetConflict(row[0])

        # Insert new ruleset
        q = sqlalchemy.insert(model.job_rulesets)
        await self.conn.execute(q, ruleset.for_db() | {"job_id": job_id})

    @_wrap_serialization_failure
    async def create_job_rule(self, job_id: model.UUID, rule_category: str, before_rule: int | None, rule: JobRule, ensure_ruleset: model.UUID | None = None) -> tuple[model.UUID, int]:
        """
        Creates a job rule before the given rule position, or None to append to the end.

        If ensure_ruleset is not None, and the currently-active ruleset does *not* have that ID,
        RulesetConflict will be raised.

        Returns the new ruleset ID and the new rule index.
        """
        current_ruleset = await self.get_job_ruleset(job_id, for_update = True)
        if ensure_ruleset is not None:
            if ensure_ruleset != current_ruleset.job_ruleset_id:
                raise RulesetConflict(current_ruleset.job_ruleset_id)
        col: RulesetColumn = getattr(current_ruleset, rule_category)
        if before_rule is None:
            col.rules.append(rule)
            position = len(col.rules) - 1
        else:
            if before_rule >= len(col.rules) or before_rule < 0:
                raise NoSuchThingError(before_rule)
            col.rules.insert(before_rule, rule)
            position = before_rule
        new_id = generate_id()
        current_ruleset.job_ruleset_id = new_id
        await self.new_ruleset(job_id, current_ruleset)
        return new_id, position

    @_wrap_serialization_failure
    async def remove_job_rule(self, job_id: model.UUID, rule_category: str, index: int, ensure_ruleset: model.UUID | None = None) -> tuple[model.UUID, JobRule]:
        """
        Removes a given job rule. If the rule does not exist, raises NoSuchThingError.

        Raises RulesetConflict if the ensure_ruleset check fails.
        Returns the new ruleset ID and the old rule value.
        """
        current_ruleset = await self.get_job_ruleset(job_id, for_update = True)
        if ensure_ruleset is not None:
            if ensure_ruleset != current_ruleset.job_ruleset_id:
                raise RulesetConflict(current_ruleset.job_ruleset_id)
        if index < 0:
            raise NoSuchThingError(index)
        col: RulesetColumn = getattr(current_ruleset, rule_category)
        try:
            old_val = col.rules.pop(index)
        except IndexError:
            raise NoSuchThingError(index)
        new_id = generate_id()
        current_ruleset.job_ruleset_id = new_id
        await self.new_ruleset(job_id, current_ruleset)
        return new_id, old_val


    @_wrap_serialization_failure
    async def remove_job_rules_by_scope(self, job_id: model.UUID, rule_category: str, scope: str, ensure_ruleset: model.UUID | None = None) -> tuple[model.UUID, int]:
        """
        Remove all job rules with the given scope.
        Returns the new ruleset ID and the number of rules removed.
        (If no values are removed, it will return the existing ruleset ID instead.)

        Raises RulesetConflict if the ensure_ruleset check fails.
        """
        current_ruleset = await self.get_job_ruleset(job_id, for_update = True)
        values_removed = 0
        if ensure_ruleset is not None:
            if ensure_ruleset != current_ruleset.job_ruleset_id:
                raise RulesetConflict(current_ruleset.job_ruleset_id)
        new_rules = []
        old_col: RulesetColumn = getattr(current_ruleset, rule_category)
        for rule in old_col.rules:
            if rule.scope == scope:
                values_removed += 1
                continue
            new_rules.append(rule)
        if not values_removed:
            return current_ruleset.job_ruleset_id, 0
        current_ruleset.job_ruleset_id = generate_id()
        old_col.rules = new_rules
        await self.new_ruleset(job_id, current_ruleset)
        return current_ruleset.job_ruleset_id, values_removed

    @classmethod
    def _page_depth(cls, page_id):
        return (
            sqlalchemy.select(sqlalchemy.func.min(model.relations.c.depth))
            .where(model.relations.c.page_id == page_id)
            .scalar_subquery()
        )

    @classmethod
    def _job_depth(cls, job_id: model.UUID):
        return (
            sqlalchemy.select(model.jobs.c.depth)
            .where(model.jobs.c.job_id == job_id)
            .scalar_subquery()
        )

    @classmethod
    def _all_pending_pages_q(cls, job_id: model.UUID):
        return (
            sqlalchemy.select(model.pages.c.page_id, model.pages.c.payload)
            .where(model.pages.c.job_id == job_id)
            .where(model.pages_dequeue_filter)
            .where(
                (cls._page_depth(model.pages.c.page_id) <= cls._job_depth(job_id))
                | (cls._job_depth(job_id) == None)
            )
            .order_by(*model.pages_dequeue_order)
        )

    async def all_pending_pages(self, job_id: model.UUID, *, _page_id: model.UUID | None = None):
        """
        Returns all pending pages for a job. Pages with a skip setting active that
        have not yet been set to the SKIPPED status are included and will need to
        be filtered out by the caller.

        If _page_id is not None, only that page ID will be returned. This is useful for tests.

        Yields a PendingPage object for every page.
        """
        ruleset = await self.get_job_ruleset(job_id)
        select_query = self._all_pending_pages_q(job_id)
        if _page_id is not None:
            select_query = select_query.where(model.pages.c.page_id == _page_id)

        async with self.conn.stream(select_query) as stream:
            async for row in stream:
                page_id, payload = row
                settings = PageSettings.from_ruleset(payload, ruleset)
                yield PendingPage(page_id, payload, ruleset.job_ruleset_id, settings)

class Pipeline:
    """
    Note: The connection is not designed to survive an error that is not
    mentioned in its docstring. If a method raises an undocumented exception,
    the transaction should be rolled back.
    """
    def __init__(self, parent: Connection, pipeline_id: str):
        """
        This constructor is not public API
        """
        self.parent = parent
        self.conn = parent.conn
        self.pipeline_id = pipeline_id

    @_wrap_serialization_failure
    async def info(self, slot: int) -> PipelineInfo:
        """
        Gets pipeline information and the current claim for a pipeline.
        """
        q = (
            sqlalchemy.select(model.pipelines.c.matchonly, model.claims.c.job_id, model.claims.c.lock)
            .join(model.claims, (model.pipelines.c.pipeline_id == model.claims.c.pipeline_id) & (model.claims.c.slot == slot), isouter = False)
            .where(model.pipelines.c.pipeline_id == self.pipeline_id)
        )
        #if lock_claim:
        #    q = q.with_for_update(of = model.claims, key_share = True)
        result = await self.conn.execute(q)
        data = result.first()
        if not data:
            raise NoSuchPipelineError("Pipeline slot should be registered before use")
        matchonly, job_id, lock = data
        return PipelineInfo(matchonly = matchonly, current_claim = (job_id, lock))

    async def _find_claimable_job(self, matchonly: bool, include_existing: model.UUID | None) -> model.UUID | None:
        """
        Finds and returns a job to claim for a particular pipeline.
        This doesn't actually claim the job, only selects one.

        include_existing is the existing claimed job. If this is specified, that job can have reached its concurrency
        limit if it is still the first in the queue order and its tag still applies.
        Otherwise, another job (or none!) will still be picked.

        If no suitable job can be found, including include_existing, returns None.
        """
        q = _find_claimable_job_q(self.pipeline_id, matchonly, include_existing, True)
        cursor = await self.conn.execute(q)
        res = cursor.first()
        if res:
            return res[0]
        return None

    @_wrap_serialization_failure
    async def _set_claim(self, slot: int, job: model.UUID | None):
        """
        Sets the current claim for a job, and updates the claim count of the old and new jobs.
        Removes any claim lock if present.
        """
        q = (
            sqlalchemy.update(model.claims)
            .where(model.claims.c.pipeline_id == self.pipeline_id)
            .where(model.claims.c.slot == slot)
            .values(job_id = job, lock = None)
            .returning(sqlalchemy.text("old.job_id"))
        )
        res = (await self.conn.execute(q)).first()
        if not res:
            raise NoSuchPipelineError()
        old_job = res[0]
        if old_job == job:
            # no need to change active claims, nothing has changed
            return
        # TODO: Combine these into one query
        if old_job:
            q = (
                sqlalchemy.update(model.jobs)
                .where(model.jobs.c.job_id == old_job)
                .values(active_claims = model.jobs.c.active_claims - 1)
            )
            await self.conn.execute(q)
        if job:
            q = (
                sqlalchemy.update(model.jobs)
                .where(model.jobs.c.job_id == job)
                .values(active_claims = model.jobs.c.active_claims + 1)
            )
            await self.conn.execute(q)

    @_wrap_serialization_failure
    async def _create_attempt(self, page: model.UUID, pipeline_version: str, ruleset_id: model.UUID) -> model.UUID:
        """
        Creates an attempt for a page, returning its ID.
        """
        ident = uuid.uuid7()
        q = sqlalchemy.insert(model.attempts)
        val = dict(
            attempt_id = ident,
            page_id = page,
            pipeline_id = self.pipeline_id,
            pipeline_version = pipeline_version,
            ruleset_id = ruleset_id,
        )
        await self.conn.execute(q, (val,))
        return ident

    @_wrap_serialization_failure
    async def _find_claimable_page(self, job_id: model.UUID, *, _page_id: model.UUID | None = None) -> PendingPage | None:
        """
        Finds a page to claim for a particular job, returning None if nothing was found.

        If _page_id is not None, either that specific page will be returned (if eligible), or nothing.
        This is used during unit testing.

        Returns a tuple of (page_id, payload, ruleset_id, page_settings).
        """
        to_skip = []
        async for page in self.parent.all_pending_pages(job_id, _page_id = _page_id):
            if page.page_settings.skip:
                to_skip.append(page.page_id)
            else:
                return page

        if to_skip:
            update_query = (
                sqlalchemy.update(model.pages)
                .values(status = model.PageStatus.SKIPPED)
                .where(model.pages.c.page_id.in_(to_skip))
            )
            await self.conn.execute(update_query)

    @_wrap_serialization_failure
    async def _claim_page(self, job_id: model.UUID, pipeline_version: str) -> PageClaimInfo | None:
        """
        Claims a page from a particular job.
        """
        info = await self._find_claimable_page(job_id)
        if info is None:
            raise JobExhausted(job_id)
        q = (
            sqlalchemy.update(model.pages)
            .where(model.pages.c.page_id == info.page_id)
            .values(
                status = model.PageStatus.CLAIMED,
                attempts = model.pages.c.attempts + 1,
                attempts_remaining = model.pages.c.attempts_remaining - 1,
            )
        )
        await self.conn.execute(q)
        attempt = await self._create_attempt(info.page_id, pipeline_version, info.ruleset_id)
        return PageClaimInfo(
            page_id = info.page_id,
            attempt_id = attempt,
            job_id = job_id,
            payload = info.payload,
            settings = info.page_settings,
            ruleset_id = info.ruleset_id,
        )

    @_wrap_serialization_failure
    async def find_claim_page(self, pipeline_version: str, slot: int) -> PageClaimInfo | None:
        """
        Claims a page.

        Specifically, this function:
        1. Gets the current claim.
        2. If the current claim is not locked, finds a new claim (if a more-appealing one exists) and disclaims the old one.
        3. Gets a page from whichever job is now claimed.

        Returns a PageClaimInfo object if a page was found; otherwise returns None.
        """
        pipeline_info = await self.info(slot)
        assert pipeline_info is not None, "Pipeline disappeared"
        current_claim, current_lock = pipeline_info.current_claim
        if current_lock is None:
            # No lock exists, try to find a better job
            new_job = await self._find_claimable_job(pipeline_info.matchonly, current_claim)
            if new_job and new_job != pipeline_info.current_claim[0]:
                # Found a better option, let's claim that
                await self._set_claim(slot, new_job)
                current_claim = new_job
        if current_claim:
            return await self._claim_page(current_claim, pipeline_version)

    @_wrap_serialization_failure
    async def create_tags(self, *tags):
        """
        Assigns tags to a pipeline. Tags that already exist are ignored.
        """
        q = (
            sqlalchemy.dialects.postgresql.insert(model.tags)
            .on_conflict_do_nothing()
        )
        values = []
        for tag in tags:
            values.append(dict(pipeline_id = self.pipeline_id, tag = tag))
        await self.conn.execute(q, values)

    @_wrap_serialization_failure
    async def remove_tags(self, *tags) -> int:
        """
        Removes tags from a pipeline. Tags that don't exist are ignored.

        Returns the number of tags actually removed.
        """
        q = model.tags.delete().where(model.tags.c.pipeline_id == self.pipeline_id).where(model.tags.c.tag.in_(tags))
        res = await self.conn.execute(q)
        return res.rowcount

    @_wrap_serialization_failure
    async def get_tags(self) -> set[str]:
        """
        Returns the tags associated with a pipeline.
        """
        q = sqlalchemy.select(model.tags.c.tag).where(model.tags.c.pipeline_id == self.pipeline_id)
        res = await self.conn.execute(q)
        return {row[0] for row in res.all()}

    @_wrap_serialization_failure
    async def authenticate(self, password: str):
        """
        Authenticates the pipeline, raising AuthenticationFailure if the pipeline doesn't exist or the password doesn't match.
        """
        q = sqlalchemy.select(model.pipelines.c.pipeline_secret).where(model.pipelines.c.pipeline_id == self.pipeline_id)
        result = await self.conn.execute(q)
        hash = result.first()
        if not hash:
            raise AuthenticationFailure
        try:
            assert hasher.verify(hash[0], password) is True
        except argon2.exceptions.VerifyMismatchError:
            raise AuthenticationFailure

    @_wrap_serialization_failure
    async def _complete_attempt(self, attempt_id: model.UUID, error: str | None) -> model.UUID:
        """
        Marks an attempt as complete, returning the page ID.
        """
        q = (
            sqlalchemy.update(model.attempts)
            .where(model.attempts.c.attempt_id == attempt_id)
            .values(finished = True, error = error)
            .returning(model.attempts.c.page_id)
        )
        res = await self.conn.execute(q)
        row = res.first()
        if not row:
            raise NoSuchThingError
        return row[0]

    async def _set_page_status_q(self, page_id: model.UUID, allow_retry: bool) -> int:
        """
        Updates a page status after being finished.
        Returns the new value of attempts_remaining.
        """
        # If a page has been SKIPPED or STASHED, don't return it to READY.
        # Writing this out the SQLAlchemy way seems to be borked. I see "expected
        # str, got PageStatus" when trying.
        # I think this is a bug in SQLAlchemy because in the stack trace, CLAIMED
        # supposedly becomes the literal string "CLAIMED". But READY doesn't, it's
        # reported as just being a PageStatus.
        new_status = sqlalchemy.text("CASE WHEN status = 'CLAIMED' THEN 'READY' ELSE status END")

        values = dict(status = new_status)
        if not allow_retry:
            values |= dict(attempts_remaining = 0)
        q = (
            sqlalchemy.update(model.pages)
            .where(model.pages.c.page_id == page_id)
            .values(values)
            .returning(model.pages.c.attempts_remaining)
        )
        res = await self.conn.execute(q)
        row = res.first()
        assert row
        return row[0]

    @_wrap_serialization_failure
    async def finish_attempt(self, attempt_id: model.UUID):
        """
        Marks an attempt as completed successfully.
        """
        page_id = await self._complete_attempt(attempt_id, None)
        await self._set_page_status_q(page_id, False)

    @_wrap_serialization_failure
    async def fail_attempt(self, attempt_id: model.UUID, error: str, fatal: bool) -> int:
        """
        Fails an attempt.
        Returns the new value of attempts_remaining.
        """
        page_id = await self._complete_attempt(attempt_id, error)
        return await self._set_page_status_q(page_id, not fatal)

    @_wrap_serialization_failure
    async def create_result(self, attempt_id: model.UUID, result_id: model.UUID, result_type: model.ResultType, payload: typing.Any):
        """
        Creates a result with the given result ID.
        Note: If a result already exists with that ID, this method will silently do nothing.
        """
        q = sqlalchemy.dialects.postgresql.insert(model.results).on_conflict_do_nothing()
        val = dict(
            result_id = result_id,
            attempt_id = attempt_id,
            type = result_type,
            payload = payload,
        )
        await self.conn.execute(q, [val])
