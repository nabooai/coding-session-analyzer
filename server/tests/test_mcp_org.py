"""D1: the MCP org read must FAIL CLOSED in team mode.

`mcp_server._org` was the only fail-open org read in the codebase — it ended `except Exception: pass`
then `return store.DEFAULT_ORG`, so any breakage in ctx injection or in the ASGI gate's header stamp
would have silently served every hosted tenant out of the DEFAULT corpus: a cross-tenant read with no
error and a green health check.

Importing mcp_server has import-time side effects (it opens/creates `data/analyzer.db` and builds the
FTS table), so every test here goes through the `mcp_srv` fixture, which chdirs into tmp_path FIRST.
Importing it at module scope would seed a `data/` dir wherever pytest happens to run."""
import pytest


@pytest.fixture(scope="module")
def mcp_srv(tmp_path_factory):
    """Import mcp_server with cwd inside a tmp dir, so its boot-time store.sqlite_db() lands there."""
    import os
    tmp = tmp_path_factory.mktemp("mcpbox")
    prev = os.getcwd()
    os.chdir(tmp)
    try:
        import mcp_server
        yield mcp_server
    finally:
        os.chdir(prev)


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _ReqCtx:
    def __init__(self, request):
        self.request = request


class _Ctx:
    """Shaped like the FastMCP Context the tools actually receive."""
    def __init__(self, headers=None, request=True):
        self.request_context = _ReqCtx(_Req(headers or {}) if request else None)


# --- stdio: the single-user local install legitimately IS the default org -------------------------

def test_stdio_returns_default_without_a_request(mcp_srv, monkeypatch):
    """FastMCP ALWAYS injects a Context; on stdio its request_context.request is just None. That is
    indistinguishable from "the gate did not stamp" if you branch on ctx/req, which is why the branch
    is on the transport flag instead. Raising here would break every local install."""
    monkeypatch.setattr(mcp_srv, "_HTTP", False)
    assert mcp_srv._org(_Ctx(request=False)) == "default"
    assert mcp_srv._org(None) == "default"


# --- team mode (--http): a missing stamp is a REFUSAL, never a default ----------------------------

def test_http_unstamped_request_raises(mcp_srv, monkeypatch):
    monkeypatch.setattr(mcp_srv, "_HTTP", True)
    with pytest.raises(RuntimeError, match="did not stamp"):
        mcp_srv._org(_Ctx(headers={}))


def test_http_missing_ctx_raises(mcp_srv, monkeypatch):
    """The regression this deliverable exists for: no ctx used to mean "the default corpus"."""
    monkeypatch.setattr(mcp_srv, "_HTTP", True)
    with pytest.raises(RuntimeError):
        mcp_srv._org(None)


def test_http_absent_request_raises(mcp_srv, monkeypatch):
    monkeypatch.setattr(mcp_srv, "_HTTP", True)
    with pytest.raises(RuntimeError):
        mcp_srv._org(_Ctx(request=False))


def test_http_broken_ctx_raises_instead_of_defaulting(mcp_srv, monkeypatch):
    """A ctx whose shape changed under us (a FastMCP rename) must refuse, not fall back. The old
    `except Exception: pass` swallowed exactly this and returned the default corpus."""
    monkeypatch.setattr(mcp_srv, "_HTTP", True)

    class Weird:
        @property
        def request_context(self):
            raise AttributeError("request_context moved")

    with pytest.raises(AttributeError):
        mcp_srv._org(Weird())


def test_http_stamped_org_is_honoured(mcp_srv, monkeypatch):
    """The gate stamps the org from the verified credential; a hosted tenant still resolves normally."""
    monkeypatch.setattr(mcp_srv, "_HTTP", True)
    assert mcp_srv._org(_Ctx(headers={"x-csa-org": "acme"})) == "acme"
    assert mcp_srv._org(_Ctx(headers={"x-csa-org": "default"})) == "default"


def test_http_malformed_org_raises(mcp_srv, monkeypatch):
    """store.valid_org_id is the path-safety gate: a traversal attempt must raise, not degrade."""
    monkeypatch.setattr(mcp_srv, "_HTTP", True)
    with pytest.raises(ValueError):
        mcp_srv._org(_Ctx(headers={"x-csa-org": "../../etc"}))


def test_org_is_not_wrapped_in_a_try(mcp_srv):
    """A `try` reintroduced around the new raise would restore the fail-open this fixes, and would do
    it invisibly — the function would still look correct. Pin the source shape."""
    import inspect
    src = inspect.getsource(mcp_srv._org)
    body = src.split('"""')[2]                           # everything after the docstring
    assert "except" not in body, "no exception handling below the docstring: it would fail open again"
    assert "return store.DEFAULT_ORG" in body, "stdio still short-circuits to the local corpus"
