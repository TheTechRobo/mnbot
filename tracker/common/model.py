from sqlalchemy import ForeignKey, Index, MetaData, Table
import sqlalchemy, sqlalchemy.dialects.postgresql, sqlalchemy.event

import enum
import typing

from uuid import UUID

SCHEMA_VERSION = 1

class JobStatus(enum.Enum):
    ACTIVE = 0

    DRAINING = 1
    """The queue is empty, but some in-progress pages remain."""

    DONE = 2

    ABORTED = 3

class PageStatus(enum.Enum):
    READY = 0
    #DEFERRED = 1
    CLAIMED = 2
    SKIPPED = 3
    STASHED = 4

class ResultType(enum.Enum):
    OUTLINKS = 0
    STATUS_CODE = 1
    FINAL_URL = 2
    REQUISITES = 3
    SCREENSHOT = 4
    CUSTOM_JS_SCREENSHOT = 5
    CUSTOM_JS = 6

class Column(sqlalchemy.Column):
    inherit_cache = True

    def __init__(self, *args, **kwargs):
        kwargs['nullable'] = kwargs.get("nullable", False)
        super().__init__(*args, **kwargs)

metadata_obj = MetaData()

pipelines = Table(
    "pipelines",
    metadata_obj,
    Column("pipeline_id", sqlalchemy.Text, primary_key = True),
    Column("pipeline_secret", sqlalchemy.Text),
    Column("matchonly", sqlalchemy.Boolean),
)

# Pipeline tags, similar to ArchiveBot's substring match for selecting pipelines to run on
# (e.g. select pipelines in Canada)
# ops can create tags as needed for specific tasks if necessary
tags = Table(
    "tags",
    metadata_obj,
    Column("pipeline_id", sqlalchemy.Text, ForeignKey("pipelines.pipeline_id"), primary_key = True),
    Column("tag", sqlalchemy.Text, primary_key = True),
)

jobs = Table(
    "jobs",
    metadata_obj,
    Column("job_id", sqlalchemy.Uuid, primary_key = True),
    Column("status", sqlalchemy.Enum(JobStatus)),
    Column("active_claims", sqlalchemy.SmallInteger, default = 0),
    Column("depth", sqlalchemy.Integer, nullable = True),
    Column("concurrency", sqlalchemy.SmallInteger),
    Column("nice", sqlalchemy.Integer),
    Column("tag", sqlalchemy.Text(), nullable = True),
    Column("created_by", sqlalchemy.Text),
    Column("note", sqlalchemy.Text, nullable = True),
    Column("initial_page", sqlalchemy.Text),
    Column("metadata", sqlalchemy.dialects.postgresql.JSONB),
)
jobs_dequeue_order = (jobs.c.nice, jobs.c.job_id)
jobs_dequeue_index = Index(
    "jobs_dequeue_index",
    *jobs_dequeue_order, jobs.c.tag,
    postgresql_where = (jobs.c.status.in_((JobStatus.ACTIVE, JobStatus.DRAINING))),
)

# Array schema: JSONB of [scope: str, payload: dict]
job_rulesets = Table(
    "job_rulesets",
    metadata_obj,
    Column("job_ruleset_id", sqlalchemy.Uuid, primary_key = True),
    Column("job_id", sqlalchemy.Uuid, ForeignKey("jobs.job_id")),
    Column("rules", sqlalchemy.dialects.postgresql.ARRAY(sqlalchemy.dialects.postgresql.JSONB, dimensions = 1, zero_indexes = True)),

    Index("job_rulesets_by_job_id", "job_id"),
)

class ClaimLock(enum.Enum):
    UNTIL_FINISHED = 0
    INDEFINITELY = 1

claims = Table(
    "claims",
    metadata_obj,
    Column("pipeline_id", sqlalchemy.Text, ForeignKey("pipelines.pipeline_id"), primary_key = True),
    Column("slot", sqlalchemy.SmallInteger, primary_key = True),
    Column("job_id", sqlalchemy.Uuid, ForeignKey("jobs.job_id"), nullable = True),
    Column("lock", sqlalchemy.Enum(ClaimLock), nullable = True),

    Index("claims_index_by_job", "job_id"),
)

pages = Table(
    "pages",
    metadata_obj,
    Column("page_id", sqlalchemy.Uuid, primary_key = True),
    Column("job_id", sqlalchemy.Uuid, ForeignKey("jobs.job_id")),
    Column("payload", sqlalchemy.Text),
    # Note! This should be reset to 0 whenever attempts_remaining is manually changed
    Column("attempts", sqlalchemy.SmallInteger, default = 0),
    Column("attempts_remaining", sqlalchemy.SmallInteger),
    Column("nice", sqlalchemy.Integer),
    Column("status", sqlalchemy.Enum(PageStatus)),
)

pages_index_unique = Index("pages_unique_url", pages.c.job_id, pages.c.payload, unique = True)
pages_dequeue_order = (pages.c.nice + pages.c.attempts, pages.c.page_id)
pages_dequeue_filter = (pages.c.status == PageStatus.READY) & (pages.c.attempts_remaining > 0)
pages_dequeue_index = Index(
    "pages_dequeue_index",
    pages.c.job_id, *pages_dequeue_order,
    postgresql_where = pages_dequeue_filter | (pages.c.status == PageStatus.CLAIMED),
)

relations = Table(
    "relations",
    metadata_obj,
    Column("relation_id", sqlalchemy.Integer, sqlalchemy.Identity(), primary_key = True),
    Column("page_id", sqlalchemy.Uuid, sqlalchemy.ForeignKey(pages.c.page_id)),
    Column("job_id", sqlalchemy.Uuid, sqlalchemy.ForeignKey("jobs.job_id")),
    Column("parent_page", sqlalchemy.Uuid, sqlalchemy.ForeignKey("pages.page_id"), nullable = True),
    # Do not add code that modifies this value (or parent_page, or page_id) without adding a new trigger!
    Column("depth", sqlalchemy.Integer),

    Index("relations_by_page", "page_id", "depth"),
    Index("relations_by_job", "job_id"),
    Index("relations_by_parent", "parent_page"),
)
relations_depth_function = sqlalchemy.DDL("""
CREATE FUNCTION update_relation_depth() RETURNS trigger AS $update_relation_depth$
    DECLARE
        existing_depth INTEGER; -- The existing lowest depth for the page.
        delta_depth INTEGER;
    BEGIN
        -- Calculate the current shortest path to any parent relation.
        -- If there is no parent, assume depth is 0.
        IF NEW.parent_page IS NULL THEN
            NEW.depth := 0;
        ELSE
            SELECT MIN(depth) + 1 INTO NEW.depth FROM relations WHERE page_id = NEW.parent_page;
        END IF;
        -- Calculate the current shortest path to any relation of this page.
        SELECT MIN(depth) INTO existing_depth FROM relations WHERE page_id = NEW.page_id;
        -- If existing_depth is NULL, the page has no existing relations, so there is nothing more to do.
        IF existing_depth IS NULL THEN
            RETURN NEW;
        END IF;
        -- Calculate the difference between the new relation's depth and the current minimum for the page.
        delta_depth := NEW.depth - existing_depth;
        -- If it is negative, the existing depth is higher - we've found a shorter path and now need to
        -- update the relation's children. Otherwise, there is nothing more to do.
        IF delta_depth >= 0 THEN
            RETURN NEW;
        END IF;
        UPDATE relations
            SET depth = relations.depth + delta_depth
            WHERE relations.job_id = NEW.job_id
            AND relations.relation_id IN (
                WITH RECURSIVE CTE (page_id, relation_id) AS (
                    SELECT r.page_id, r.relation_id FROM relations AS r WHERE parent_page = NEW.page_id
                    UNION
                    SELECT r.page_id, r.relation_id FROM relations AS r
                        INNER JOIN CTE ON CTE.page_id = r.parent_page
                )
                SELECT relation_id FROM CTE
            )
        ;
        RETURN NEW;
    END
$update_relation_depth$ LANGUAGE plpgsql;
    """)
relations_depth_trigger = sqlalchemy.DDL(
    "CREATE TRIGGER relations_depth_trigger BEFORE INSERT ON relations FOR EACH ROW EXECUTE FUNCTION update_relation_depth();"
)
sqlalchemy.event.listen(relations, "after_create", relations_depth_function)
sqlalchemy.event.listen(relations, "after_create", relations_depth_trigger)

attempts = Table(
    "attempts",
    metadata_obj,
    Column("attempt_id", sqlalchemy.Uuid, primary_key = True),
    Column("page_id", sqlalchemy.Uuid, ForeignKey("pages.page_id")),
    Column("pipeline_id", sqlalchemy.Text),
    Column("pipeline_version", sqlalchemy.Text),
    Column("error", sqlalchemy.Text, nullable = True, default = None),
    Column("finished", sqlalchemy.Boolean, default = False),
    Column("ruleset_id", sqlalchemy.Uuid),

    # TODO: Does attempt_id need to be explicitly stated here?
    Index("attempts_index_by_page", "page_id", "attempt_id"),
)

# Page results
results = Table(
    "results",
    metadata_obj,
    Column("result_id", sqlalchemy.Uuid, primary_key = True),
    Column("attempt_id", sqlalchemy.Uuid, ForeignKey("attempts.attempt_id")),
    Column("type", sqlalchemy.Enum(ResultType)),
    Column("payload", sqlalchemy.dialects.postgresql.JSONB),

    Index("results_by_attempt", "attempt_id"),
)

# Global options
options = sqlalchemy.Table(
    "options",
    metadata_obj,
    Column("key", sqlalchemy.Text, primary_key = True),
    Column("value", sqlalchemy.Text),
)
