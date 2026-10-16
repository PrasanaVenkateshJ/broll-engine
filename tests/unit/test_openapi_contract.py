"""openapi.yaml must describe the running app EXACTLY (Integration Standard rule 2).

If someone adds a route, a field or an error code and forgets openapi.yaml, a test here fails.
"""
from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute, iter_route_contexts

from app.config import get_settings
from app.errors import ErrorCode
from app.jobs import callback_payload, job_summary, job_to_dict
from app.main import app
from app.models import Job
from app.schemas import JobCreate, JobOptions, SceneIn, SwapRequest
from app.security import require_client

SPEC = yaml.safe_load((Path(__file__).resolve().parents[2] / "openapi.yaml").read_text(encoding="utf-8"))
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
# The B-roll Studio web page + its static files: a user interface, not part of the API contract.
UI_ROUTES = {"/", "/studio/{filename}"}


def resolve(node):
    """Follow a local $ref like '#/components/parameters/CorrelationId'."""
    while isinstance(node, dict) and "$ref" in node:
        target = SPEC
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def app_routes() -> dict[tuple[str, str], APIRoute]:
    """(full path, method) -> route, for every route the app serves except FastAPI's own docs pages and
    the Studio UI. iter_route_contexts() also walks into included routers (the /api/v1 one) and gives full paths."""
    docs = {app.openapi_url, app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url} | UI_ROUTES
    out = {}
    for context in iter_route_contexts(app.routes):
        if context.path in docs:
            continue
        route = context.route
        assert isinstance(route, APIRoute), f"unexpected non-API route {context.path}"
        for method in route.methods - {"HEAD", "OPTIONS"}:
            out[(context.path, method.lower())] = route
    return out


def spec_operations() -> dict[tuple[str, str], dict]:
    return {(path, method): op for path, item in SPEC["paths"].items() for method, op in item.items()
            if method in HTTP_METHODS}


def test_spec_is_openapi_3_1_with_the_app_version():
    assert SPEC["openapi"].startswith("3.1.")
    assert SPEC["info"]["version"] == get_settings().APP_VERSION


def test_every_route_and_method_matches_exactly():
    assert set(app_routes()) == set(spec_operations())


def test_ui_routes_are_hidden_from_fastapis_own_schema():
    hidden = {c.path for c in iter_route_contexts(app.routes) if not c.route.include_in_schema}
    assert UI_ROUTES <= hidden


def test_all_api_routes_are_under_api_v1():
    for path, _ in app_routes():
        assert path in ("/health", "/ready") or path.startswith("/api/v1/"), path


@pytest.mark.parametrize("key", sorted(spec_operations()))
def test_success_status_auth_and_headers_match(key):
    route, op = app_routes()[key], spec_operations()[key]
    assert str(route.status_code or 200) in op["responses"], f"{key}: success status not documented"
    for code, response in op["responses"].items():
        assert "X-Correlation-Id" in resolve(response).get("headers", {}), f"{key} {code}: no X-Correlation-Id"
        if code.startswith(("4", "5")):
            schema = resolve(response)["content"]["application/json"]["schema"]
            assert schema == {"$ref": "#/components/schemas/Error"}, f"{key} {code}: not the error schema"
    params = {(p["in"], p["name"]): p for p in map(resolve, op.get("parameters", []))}
    assert ("header", "X-Correlation-Id") in params
    for name in route.param_convertors:  # path parameters
        assert ("path", name) in params, f"{key}: path parameter {name} not documented"
    # Bearer auth: the spec's security must match whether the route depends on require_client.
    needs_token = any(dep.dependency is require_client for dep in route.dependencies)
    assert (op.get("security", SPEC["security"]) != []) == needs_token, f"{key}: security does not match"
    if key[1] == "post":
        assert ("header", "Idempotency-Key") in params, f"{key}: Idempotency-Key not documented"
        assert "202" in op["responses"] and "409" in op["responses"]


def test_swap_requires_the_idempotency_key():
    op = spec_operations()[("/api/v1/broll/jobs/{job_id}/scenes/{scene_index}/swap", "post")]
    params = {p["name"]: p for p in map(resolve, op["parameters"])}
    assert params["Idempotency-Key"]["required"] is True


def test_callback_signature_header_is_documented():
    callback = SPEC["paths"]["/api/v1/broll/jobs"]["post"]["callbacks"]["jobFinished"]
    (expression,) = callback
    params = {p["name"] for p in callback[expression]["post"]["parameters"]}
    assert expression == "{$request.body#/callback_url}" and "X-IncuBrix-Signature" in params


def test_every_error_code_is_in_the_spec():
    assert SPEC["components"]["schemas"]["ErrorCode"]["enum"] == [c.value for c in ErrorCode]
    error = SPEC["components"]["schemas"]["ErrorObject"]
    assert set(error["required"]) == set(error["properties"]) == {"code", "message", "retryable", "details"}


@pytest.mark.parametrize("model, schema", [(SceneIn, "SceneIn"), (JobOptions, "JobOptions"),
                                           (SwapRequest, "SwapRequest")])
def test_request_schemas_have_the_same_fields_as_the_pydantic_models(model, schema):
    assert set(SPEC["components"]["schemas"][schema]["properties"]) == set(model.model_fields)


def test_job_create_schema_matches():
    parts = SPEC["components"]["schemas"]["JobCreate"]["allOf"]
    fields = set(resolve(parts[0])["properties"]) | set(parts[1]["properties"])
    assert fields == set(JobCreate.model_fields)


def test_job_and_callback_bodies_match_the_code():
    job = Job(id="j1", client_id="c", status="queued", progress=0, stage="queued", input_type="script",
              request_json="{}", warnings_json="[]", result_files_json="[]")
    schema = SPEC["components"]["schemas"]["Job"]
    assert set(job_to_dict(job, "http://x")) == set(schema["properties"]) == set(schema["required"])
    assert set(callback_payload(job)) == set(SPEC["components"]["schemas"]["CallbackPayload"]["properties"])
    summary = SPEC["components"]["schemas"]["JobSummary"]
    assert set(job_summary(job)) == set(summary["properties"]) == set(summary["required"])


def test_public_config_matches_the_code():
    from app.main import public_config

    schema = SPEC["components"]["schemas"]["PublicConfig"]
    assert set(public_config()) == set(schema["properties"]) == set(schema["required"])
