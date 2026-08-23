from sqlalchemy import ForeignKey, Index, MetaData, Table
import sqlalchemy, sqlalchemy.dialects.postgresql, sqlalchemy.event

import enum

from uuid import UUID

SCHEMA_VERSION = 1

class JobStatus(enum.Enum):
    ACTIVE = 0

    DRAINING = 1
    """The queue is empty, but some in-progress pages remain."""

    DONE = 2

    ABORTED = 3

    def is_done(self):
        return self in (JobStatus.ABORTED, JobStatus.DONE)

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
    Column("last_checkin", sqlalchemy.TIMESTAMP(timezone = True), nullable = True),
    Column("disk_free_bytes", sqlalchemy.BigInteger, nullable = True),
    Column("disk_total_bytes", sqlalchemy.BigInteger, nullable = True),
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

job_rulesets = Table(
    "job_rulesets",
    metadata_obj,
    Column("job_ruleset_id", sqlalchemy.Uuid, primary_key = True),
    Column("job_id", sqlalchemy.Uuid, ForeignKey("jobs.job_id")),

    Column("ua", sqlalchemy.dialects.postgresql.JSONB),
    Column("custom_js", sqlalchemy.dialects.postgresql.JSONB),
    Column("skip", sqlalchemy.dialects.postgresql.JSONB),
    Column("accept", sqlalchemy.dialects.postgresql.JSONB),

    Index("job_rulesets_by_job_id", "job_id"),
)
job_ruleset_columns = (job_rulesets.c.ua, job_rulesets.c.custom_js, job_rulesets.c.skip, job_rulesets.c.accept)

claims = Table(
    "claims",
    metadata_obj,
    Column("pipeline_id", sqlalchemy.Text, ForeignKey("pipelines.pipeline_id"), primary_key = True),
    Column("slot", sqlalchemy.SmallInteger, primary_key = True),
    Column("job_id", sqlalchemy.Uuid, ForeignKey("jobs.job_id"), nullable = True),

    Index("claims_index_by_job", "job_id"),
)

pages = Table(
    "pages",
    metadata_obj,
    Column("page_id", sqlalchemy.Uuid, primary_key = True),
    Column("job_id", sqlalchemy.Uuid, ForeignKey("jobs.job_id")),
    Column("payload", sqlalchemy.Text),
    Column("payload_ssurt", sqlalchemy.Text),
    # Note! This should be reset to 0 whenever attempts_remaining is manually changed
    Column("attempts", sqlalchemy.SmallInteger, default = 0),
    Column("attempts_remaining", sqlalchemy.SmallInteger),
    Column("nice", sqlalchemy.Integer),
    Column("status", sqlalchemy.Enum(PageStatus)),

    Index("pages_ssurt", "payload_ssurt"),
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
    Column("relation_id", sqlalchemy.BigInteger, sqlalchemy.Identity(), primary_key = True),
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

        loop_parents relations.parent_page%%TYPE[];
        loop_children relations.parent_page%%TYPE[];
        seen_relations relations.relation_id%%TYPE[];
        child_row RECORD;
        new_depth integer;
    BEGIN
        -- Retrieve the current shortest path to any relation of the parent page, then add 1. That'll be this relation's depth.
        -- If there is no parent, assume depth is 0.
        IF NEW.parent_page IS NULL THEN
            NEW.depth := 0;
        ELSE
            SELECT MIN(depth) + 1 INTO NEW.depth FROM relations WHERE page_id = NEW.parent_page;
            IF NEW.depth IS NULL THEN
                RAISE EXCEPTION 'Parent page does not exist!';
            END IF;
        END IF;

        -- Retrieve the current shortest path to any relation of this page.
        -- If the new depth isn't lower, or there are no existing relations, we've nothing to do.
        SELECT MIN(depth) INTO existing_depth FROM relations WHERE page_id = NEW.page_id;
        IF (existing_depth IS NULL) OR (NEW.depth >= existing_depth) THEN
            RETURN NEW;
        END IF;

        -- Recursively update children with their new depth, if the new depth is lower than their existing depth.
        -- Because only one relation has been inserted per trigger call, and we're doing breadth first, we can safely
        -- ignore a relation's children if its depth is unchanged.
        -- The idea is to be very similar to a cascading UPDATE trigger, but without cycles causing problems
        -- (as we store every ID we've seen in seen_relations and filter only for relations that *aren't* in there).
        seen_relations := ARRAY[]::integer[];
        new_depth := NEW.depth;
        loop_parents := ARRAY[NEW.page_id]; -- Start with the current relation.
        LOOP
            -- Once we've reached the end, this array will be empty.
            EXIT WHEN cardinality(loop_parents) = 0;
            new_depth := new_depth + 1;
            loop_children := ARRAY[]::uuid[];
            FOR child_row IN
                UPDATE relations
                SET depth = new_depth
                WHERE (parent_page = ANY(loop_parents)) AND NOT (relation_id = ANY(seen_relations)) AND (new_depth < relations.depth)
                RETURNING relation_id, page_id
            LOOP
                loop_children := array_append(loop_children, child_row.page_id);
                seen_relations := array_append(seen_relations, child_row.relation_id);
            END LOOP;
            loop_parents := loop_children;
        END LOOP;
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
    Column("pipeline_slot", sqlalchemy.SmallInteger),
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
