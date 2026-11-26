"""The API reference's "Same call elsewhere" is read from the SDK's and MCP server's code."""

from __future__ import annotations

from sitegen import calls

SDK = """
class Client:
    def __init__(self):
        self.signatures = Signatures(self)

    def me(self):
        return self.request("GET", "/me")


class Signatures(_Part):
    def list(self, ref, *, verify=False):
        return self._client.request("GET", _path("/entities/{ref}/signatures", ref=ref))

    def get(self, signature):
        return self._client.request("GET", f"/signatures/{signature}")

    def _private(self):
        return self._client.request("DELETE", "/nothing")


class Unbound(_Part):
    def ignored(self):
        return self._client.request("GET", "/unbound")
"""

MCP = """
def register(server):
    @server.tool(annotations=READ, title="Get a record's signatures")
    async def get_signatures(ref):
        return answer(await call("GET", path("/entities/{ref}/signatures", ref=ref)))

    def helper():
        return call("GET", "/not-a-tool")
"""


def test_a_call_is_its_method_and_path_without_parameter_names() -> None:
    assert calls.key("get", "/api/v1/entities/{ref}/signatures") == "GET /entities/{}/signatures"
    assert calls.key("GET", "/entities/{id}/signatures") == "GET /entities/{}/signatures"


def test_the_sdk_methods_that_make_each_call() -> None:
    assert calls.sdk_calls(SDK) == {
        "GET /me": ["client.me"],
        "GET /entities/{}/signatures": ["client.signatures.list"],
        "GET /signatures/{}": ["client.signatures.get"],
    }


def test_the_mcp_tools_that_make_each_call() -> None:
    assert calls.mcp_calls(MCP) == {"GET /entities/{}/signatures": ["get_signatures"]}


def test_the_real_sdk_and_server_make_the_signatures_call() -> None:
    found = calls.collect()
    assert found["GET /entities/{}/signatures"] == {
        "sdk": ["client.signatures.list"],
        "mcp": ["get_signatures"],
    }
    # Most of the SDK's and the MCP server's calls are found, not a stray few.
    assert sum(len(names["sdk"]) for names in found.values()) > 40
    assert sum(len(names["mcp"]) for names in found.values()) > 40
