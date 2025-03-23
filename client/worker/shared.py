import dataclasses, typing, os, logging

from result import *

logging.basicConfig(format = "%(asctime)s %(levelname)s <:%(thread)s> : %(message)s", level = logging.INFO)

# Update this whenever you make a change, cosmetic or not.
# During development you can ignore it, but when you actually
# push it to prod, it *must* be updated.
VERSION = "20250323.01"

DEBUG = os.environ.get("DEBUG") == "1"
if DEBUG:
    VERSION += "-debug"

PROXY_URL = "warcprox:8000"

# Used in IPC between app.py and browse.py.
@dataclasses.dataclass
class Job:
    full_job: dict
    url: str
    warc_prefix: str
    dedup_bucket: str
    stats_bucket: str
    stealth_ua: bool
    custom_js: typing.Optional[str]
    cookie_jar: typing.Optional[bytes]

    mnbot_info_url: str

@dataclasses.dataclass
class Result:
    final_url: str
    outlinks: list
    custom_js: typing.Optional[dict]
    status_code: int
    requisites: dict[str, Chain]

    # Create a dict to write to the WARC
    def dict(self) -> dict[str, typing.Any]:
        return {
            "final_url": self.final_url,
            "outlinks": self.outlinks,
            "custom_js_result": self.custom_js,
            "requisites": [dataclasses.asdict(v) for v in self.requisites.values()]
            # Don't include status code because that's already in the WARC
        }

    def full_dict(self) -> typing.Dict[str, typing.Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_full_dict(cls, data) -> typing.Self:
        rv = cls(**data)
        for key, value in data['requisites']:
            for i, pair in enumerate(value['chain']):
                pair['request'] = Request(**pair['request'])
                if pair['response']['_type'] == "Response":
                    pair['response'] = Response(**pair['response'])
                elif pair['response']['_type'] == "Error":
                    pair['response'] = Error(**pair['response'])
                value['chain'][i] = Pair(**pair)
            value = Chain(**value)
            rv.requisites[key] = value
        return rv
