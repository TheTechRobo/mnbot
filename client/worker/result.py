import dataclasses
import typing

@dataclasses.dataclass
class Request:
    category: str | None
    url: str
    method: str

    _type: str = "Request"

    @classmethod
    def from_params(cls, params: dict) -> typing.Self:
        req = params['request']
        return cls(
            category = params.get("type"),
            url = req['url'],
            method = req['method'],
        )

@dataclasses.dataclass
class Response:
    status: tuple[int, str]
    mimetype: str
    length: int

    _type: str = "Response"

    @classmethod
    def from_params(cls, params: dict) -> typing.Self:
        resp = params['response']
        return cls.from_dict(resp)

    @classmethod
    def from_dict(cls, resp: dict) -> typing.Self:
        return cls(
            status = (resp['status'], resp['statusText']),
            mimetype = resp['mimeType'],
            # encodedDataLength is just the length of the headers
            #length = resp['encodedDataLength']
            length = 0
        )

@dataclasses.dataclass
class Error:
    text: str

    _type: str = "Error"

@dataclasses.dataclass
class Pair:
    """
    Request/response pair.
    """
    request: Request
    response: Response | Error | None = None

@dataclasses.dataclass
class Chain:
    """
    Corresponds to one requisite in the `requisites` result.
    A chain of request/response pairs. (It's a chain due to possible redirects.)
    """
    chain: list[Pair]
    id: str
