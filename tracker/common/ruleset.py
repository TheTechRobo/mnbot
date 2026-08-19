import dataclasses
import typing
import regex

from . import model

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

    def matches(self, url):
        return bool(self._compiled_scope.search(url, timeout = 15))

RulesetPayload = typing.TypeVar("RulesetPayload")

@dataclasses.dataclass
class RulesetColumn[RulesetPayload]:
    default: RulesetPayload
    rules: list[JobRule[RulesetPayload]]

    def compute_for(self, url: str) -> RulesetPayload:
        item = self.default
        for rule in self.rules:
            if rule.matches(url):
                item = rule.payload
        return item

    def add(self, rule: JobRule, before_rule: int | None = None) -> int:
        """
        Creates a job rule before the given rule position, or None to append to the end.
        Raises IndexError if before_rule is less than 0 or greater than len(rules).

        Returns the new rule index.
        """
        if before_rule is None:
            self.rules.append(rule)
            position = len(self.rules) - 1
        else:
            if before_rule >= len(self.rules) or before_rule < 0:
                raise IndexError(before_rule)
            self.rules.insert(before_rule, rule)
            position = before_rule
        return position

    def remove(self, index: int) -> JobRule:
        """
        Removes a given job rule. If the rule does not exist, raises IndexError.

        Returns the old rule value.
        """
        if index < 0:
            raise IndexError(index)
        old_val = self.rules.pop(index)
        return old_val

    def remove_by_scope(self, scope: str) -> int:
        """
        Remove all job rules with the given scope.
        Returns the number of rules removed.
        """
        values_removed = 0
        new_rules = []
        for rule in self.rules:
            if rule.scope == scope:
                values_removed += 1
                continue
            new_rules.append(rule)
        self.rules = new_rules
        return values_removed

@dataclasses.dataclass
class JobRuleset:
    job_ruleset_id: model.UUID
    """This ID is not written into the database when calling new_ruleset."""

    all_columns = ("ua", "custom_js", "skip", "accept")

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
        kwargs = {k: cls._make_column(getattr(row, k)) for k in cls.all_columns}
        return cls(job_ruleset_id = row.job_ruleset_id, **kwargs)

    def for_db(self):
        return {k: self._encode_column(getattr(self, k)) for k in self.all_columns}

    def remove_all_by_scope(self, scope: str) -> int:
        removed = 0
        for key in self.all_columns:
            removed += getattr(self, key).remove_by_scope(scope)
        return removed

@dataclasses.dataclass
class PageSettings:
    ua: str
    custom_js: str | None
    skip: bool
    accept: bool

    @classmethod
    def from_ruleset(cls, url: str, ruleset: JobRuleset) -> typing.Self:
        kwargs = {}
        for key in JobRuleset.all_columns:
            kwargs[key] = getattr(ruleset, key).compute_for(url)
        return cls(**kwargs)

    def as_dict(self):
        return dataclasses.asdict(self)
