"""Request templates: placeholders, encoding by position, same-origin URLs and safe JSON bodies."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import pytest
from yarl import URL

from deltasleuth.connectors.base import ConnectorConfigError, KeyRejectedError, UnsafeTargetError
from deltasleuth.connectors.templating import Endpoint, RenderedRequest, build_request, template_variables
from deltasleuth.core.types import RequestTemplate

ENDPOINT = Endpoint.parse("http://svc.example:8080/api")


def build(
    path: str = "/things/{{ key }}", key: str = "K1", *, auth: dict[str, str] | None = None, **template: Any
) -> RenderedRequest:
    method = template.pop("method", "POST" if "body" in template else "GET")
    page_params = template.pop("page_params", ())
    parts = template.get("key_parts", ())  # a multi-part key's part names
    return build_request(
        ENDPOINT,
        RequestTemplate(method=method, path=path, **template),
        template_variables(key, "run", 1, parts),
        auth_headers=auth or {},
        page_params=page_params,
    )


class TestPlaceholders:
    @pytest.mark.parametrize("text", ["{{key}}", "{{ key }}", "{{  key  }}", "{{ key}}"])
    def test_spacing_variants(self, text: str) -> None:
        assert build(headers={"X-K": text}).headers["X-K"] == "K1"

    def test_variables(self) -> None:
        req = build(headers={"X-Correlation-Id": "ds-{{ run_id }}-{{ call_seq }}"})
        assert req.headers["X-Correlation-Id"] == "ds-run-1"
        assert template_variables("K", "run", 5) == {"key": "K", "run_id": "run", "call_seq": "5"}

    @pytest.mark.parametrize("name", ["secret", "cycle_no", "keyParts.code"])
    def test_unknown_placeholder_rejected(self, name: str) -> None:
        with pytest.raises(ConnectorConfigError, match="unknown placeholder"):
            build(headers={"X-K": "{{ " + name + " }}"})

    @pytest.mark.parametrize("text", ["{{ key }", "{{ key | upper }}", "x }} y", "{{ }}"])
    def test_malformed_braces_fail_loudly(self, text: str) -> None:
        with pytest.raises(ConnectorConfigError):
            build(headers={"X-K": text})

    @pytest.mark.parametrize("key", ["", ".", "..", 12])
    def test_unsendable_keys(self, key: object) -> None:
        with pytest.raises(KeyRejectedError):
            template_variables(key, "run", 1)  # type: ignore[arg-type]


class TestPath:
    @pytest.mark.parametrize(
        ("key", "segment"),
        [
            ("a/b", "a%2Fb"),
            ("a b", "a%20b"),
            ("é", "%C3%A9"),
            ("a?b#c", "a%3Fb%23c"),
            ("%2e%2e", "%252e%252e"),
            ("...", "..."),
            (".a", ".a"),
        ],
    )
    def test_key_is_one_path_segment(self, key: str, segment: str) -> None:
        assert build(key=key).url.raw_path == f"/api/things/{segment}"

    @pytest.mark.parametrize(
        ("path", "raw"),
        [
            ("/café/{{ key }}", "/api/caf%C3%A9/K1"),
            ("/a/b/", "/api/a/b/"),
            ("/a/%2e%2e%2e", "/api/a/%2e%2e%2e"),
        ],
    )
    def test_literal_path_text(self, path: str, raw: str) -> None:
        assert build(path).url.raw_path == raw

    @pytest.mark.parametrize("path", ["/things?x=1", "/things#f", "/a b/{{ key }}", "/{x}"])
    def test_forbidden_literal_path_text(self, path: str) -> None:
        with pytest.raises(ConnectorConfigError):
            build(path)

    @pytest.mark.parametrize(
        "path",
        [
            "//evil.example/{{ key }}",
            "/../admin/{{ key }}",
            "/a/%2e%2E/{{ key }}",
            "/a/./b",
            "/a//b",
            "things",
        ],
    )
    def test_template_cannot_leave_base_path(self, path: str) -> None:
        with pytest.raises(UnsafeTargetError):
            build(path)


class TestQuery:
    def test_component_encoded_lists_repeat_scalars_render(self) -> None:
        query = {
            "q": "{{ key }}",
            "tags": ["x y", "&"],
            "on": True,
            "off": False,
            "n": 3,
            "d": Decimal("1.50"),
        }
        req = build(query={**query, "f": 0.5}, key="a/b&c")
        assert req.url.raw_query_string == "q=a%2Fb%26c&tags=x%20y&tags=%26&on=true&off=false&n=3&d=1.5&f=0.5"

    def test_page_params_replace_template_parameters(self) -> None:
        req = build(query={"page": 5, "q": "x"}, page_params=(("page", "1"), ("size", "10")))
        assert req.url.raw_query_string == "q=x&page=1&size=10"

    def test_non_scalar_rejected(self) -> None:
        with pytest.raises(ConnectorConfigError):
            build(query={"q": {"nested": 1}})


class TestHeaders:
    def test_auth_added(self) -> None:
        req = build(headers={"Accept": "application/json"}, auth={"Authorization": "Bearer t"})
        assert req.headers == {"Accept": "application/json", "Authorization": "Bearer t"}

    @pytest.mark.parametrize(
        "name",
        ["Authorization", "cookie", "X-Api-Key", "Host", "Content-Length", "Bad Header", "X-A:b", "", "X\nY"],
    )
    def test_reserved_or_invalid_names_rejected(self, name: str) -> None:
        with pytest.raises(ConnectorConfigError):
            build(headers={name: "v"})

    def test_custom_auth_header_cannot_be_overridden(self) -> None:
        with pytest.raises(ConnectorConfigError):
            build(headers={"x-service-token": "v"}, auth={"X-Service-Token": "s"})

    def test_header_injection_through_key_rejected(self) -> None:
        with pytest.raises(KeyRejectedError):
            build(headers={"X-Key": "{{ key }}"}, key="a\r\nX-Evil: 1")


class TestJsonBody:
    def test_body_rendered_with_exact_numbers(self) -> None:
        req = build("/q", body={"code": "{{ key }}", "limit": Decimal("10.50"), "tags": ["{{ key }}"]})
        assert req.body == b'{"code": "K1", "limit": 10.5, "tags": ["K1"]}'
        assert req.headers["Content-Type"] == "application/json"

    @pytest.mark.parametrize(
        "key",
        [
            'x", "role": "admin',  # would close the string and add a member if spliced as text
            'x"}, "extra": {"a": "',
            "back\\slash\nnew line\u2028",
            "{{ run_id }}",  # a value is data: it is never rendered again
        ],
    )
    def test_graphql_key_is_json_data_never_json_text(self, key: str) -> None:
        query = "query($id: ID!) { character(id: $id) { name } }"
        body = {"query": query, "variables": {"id": "{{ key }}", "label": "k-{{ key }}"}}
        sent = json.loads(build("/graphql", key=key, body=body).body or b"")
        assert sent == {"query": query, "variables": {"id": key, "label": f"k-{key}"}}

    def test_member_names_are_not_templated(self) -> None:
        assert json.loads(build("/q", body={"{{ key }}": 1}).body or b"") == {"{{ key }}": 1}

    def test_explicit_content_type_kept(self) -> None:
        req = build("/q", body={}, headers={"content-type": "application/x+json"})
        assert req.headers == {"content-type": "application/x+json"}

    @pytest.mark.parametrize(("method", "body"), [("GET", {"a": 1}), ("DELETE", None)])
    def test_only_post_carries_a_body_and_only_reads_are_sent(self, method: str, body: object) -> None:
        with pytest.raises(ConnectorConfigError):
            build(method=method, body=body)


class TestKeyParts:
    """A multi-part key: each part renders as ``{{ keyParts.<name> }}``, encoded as ``{{ key }}`` is."""

    PARTS = ("region", "shelf", "code")

    def test_key_parts_render_everywhere(self) -> None:
        key = "EU|a b&c|x\\|y"  # the last part is x|y
        req = build(
            "/r/{{ keyParts.region }}/s/{{ keyParts.shelf }}",
            key,
            key_parts=self.PARTS,
            query={"code": "{{ keyParts.code }}", "all": "{{ key }}"},
            headers={"X-Shelf": "s-{{ keyParts.shelf }}"},
            body={"item": "{{ keyParts.code }} in {{ keyParts.region }}", "key": "{{ key }}", "n": 1},
        )
        assert req.url.raw_path == "/api/r/EU/s/a%20b%26c"
        assert req.url.raw_query_string == "code=x%7Cy&all=EU%7Ca%20b%26c%7Cx%5C%7Cy"
        assert req.headers["X-Shelf"] == "s-a b&c"
        assert json.loads(req.body or b"") == {"item": "x|y in EU", "key": key, "n": 1}
        assert template_variables(key, "run", 5, self.PARTS) == {
            "key": key, "run_id": "run", "call_seq": "5",
            "keyParts.region": "EU", "keyParts.shelf": "a b&c", "keyParts.code": "x|y",
        }  # fmt: skip
        with pytest.raises(ConnectorConfigError, match="unknown placeholder"):  # no part of this key
            build("/r/{{ keyParts.aisle }}", key, key_parts=self.PARTS)

    @pytest.mark.parametrize(
        ("part", "segment"),
        [
            ("a/b", "a%2Fb"),
            ("a/../b", "a%2F..%2Fb"),
            ("%2e%2e", "%252e%252e"),
            ("a?b#c", "a%3Fb%23c"),
            ("...", "..."),
        ],
    )
    def test_a_slash_in_a_part_stays_one_segment(self, part: str, segment: str) -> None:
        req = build("/r/{{ keyParts.shelf }}/t/{{ keyParts.code }}", f"EU|{part}|7", key_parts=self.PARTS)
        assert req.url.raw_path == f"/api/r/{segment}/t/7"

    @pytest.mark.parametrize(
        "key", ["EU|.|7", "EU|..|7", "EU||7", "|s|7", "EU|s|", "EU|s", "EU|s|7|x", "EU|s|7\\"]
    )
    def test_a_dot_part_is_rejected(self, key: str) -> None:
        # Whatever the template reads: a key some of whose parts cannot be sent is sent by none.
        with pytest.raises(KeyRejectedError, match=r"not its parts joined by \|, or a part is empty"):
            build("/things/{{ key }}", key, key_parts=self.PARTS)
        sent = build("/things/{{ key }}", key)  # a key of one part: sent whole
        assert sent.url.raw_path == "/api/things/" + quote(key, safe="")


class TestEndpoint:
    def test_parse(self) -> None:
        ep = Endpoint.parse("https://Svc.Example:8443/api/v1/")
        assert (ep.scheme, ep.host, ep.port, ep.base_path) == ("https", "svc.example", 8443, "/api/v1")
        assert Endpoint.parse("http://svc.example").port == 80

    @pytest.mark.parametrize(
        "base",
        [
            "",
            "ftp://svc.example/",
            "/relative",
            "http://user:pw@svc.example/",
            "http://svc.example/?q=1",
            "http://svc.example/#frag",
        ],
    )
    def test_invalid_base_urls(self, base: str) -> None:
        with pytest.raises(ConnectorConfigError):
            Endpoint.parse(base)

    @pytest.mark.parametrize(
        ("url", "admitted"),
        [
            ("http://svc.example:8080/api/x?y=1", True),
            ("http://svc.example:8080/api", True),
            ("http://svc.example:8080/apix", False),  # a prefix, not a segment
            ("http://svc.example:8080/other", False),
            ("https://svc.example:8080/api/x", False),
            ("http://svc.example:9090/api/x", False),
            ("http://evil.example:8080/api/x", False),
            ("http://u:p@svc.example:8080/api/x", False),
            ("http://svc.example:8080/api/x#frag", False),
            ("http://svc.example:8080/api/%2e%2e/admin", False),
            ("http://svc.example:8080/api//x", False),
        ],
    )
    def test_admits_only_the_origin_under_the_base_path(self, url: str, admitted: bool) -> None:
        assert ENDPOINT.admits(URL(url, encoded=True)) is admitted
