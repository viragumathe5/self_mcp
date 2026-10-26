"""Local-only onboarding API. Never reflect request or upstream secret values."""

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.concurrency import run_in_threadpool

import providers
import provider_transport


settings = providers.ProviderSettings()


def guard_request(request: Request) -> None:
    # Uvicorn can rewrite the peer/scheme using proxy headers. Reject those headers
    # outright instead of trusting the rewritten values for local authorization.
    if any(name.lower().startswith("x-forwarded-") or name.lower() in ("forwarded", "x-real-ip") for name in request.headers):
        raise HTTPException(403, "Forwarded requests are not allowed for provider configuration.")
    try:
        if request.client is None or not ipaddress.ip_address(request.client.host).is_loopback:
            raise ValueError
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or not re.fullmatch(r"(?:localhost|127(?:\.[0-9]{1,3}){3}|\[::1\])(?::[0-9]{1,5})?", hosts[0], re.IGNORECASE):
            raise ValueError
        host = urlsplit(f"//{hosts[0]}")
        if host.hostname.lower() != "localhost" and not ipaddress.ip_address(host.hostname).is_loopback:
            raise ValueError
        if host.port is not None and not 1 <= host.port <= 65535:
            raise ValueError
        origins = request.headers.getlist("origin")
        if origins and origins != [f"{request.scope['scheme']}://{hosts[0]}"]:
            raise ValueError
    except ValueError:
        raise HTTPException(403, "Provider configuration requires a loopback client and Host, and an exactly matching Origin (or no Origin for CLI clients).") from None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        types = request.headers.getlist("content-type")
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "Provider writes require Content-Type: application/json, including DELETE.")


class ProviderRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request):
            guard_request(request)
            try:
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16384:
                        raise HTTPException(413, "Provider request body is too large.")
                request._body = bytes(body)
                response = await handler(request)
                response.headers['Cache-Control'] = 'no-store'
                return response
            except RequestValidationError:
                # FastAPI's default validation response includes raw input values.
                raise HTTPException(422, "Invalid provider request. Check field names, types, allowed values, and lengths.") from None
            except providers.ProviderError as error:
                raise HTTPException(error.status, error.message) from None

        return guarded


router = APIRouter(prefix="/api/providers", tags=["providers"], route_class=ProviderRoute)


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TestInput(ProviderInput):
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    model: str | None = Field(default=None, min_length=1, max_length=200, pattern=f"^{providers.MODEL_PATTERN}$")

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value: SecretStr | None):
        if value is not None and not re.fullmatch(r"[!-~]{1,4096}", value.get_secret_value()):
            raise ValueError("Invalid API key format")
        return value


class SaveInput(TestInput):
    storage: Literal["session", "keyring"]
    model: str = Field(default="", max_length=200)
    make_default: bool = True


class OnboardingInput(ProviderInput):
    dismissed: bool


class ProfileInput(ProviderInput):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["ollama", "openai_compatible"]
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    storage: Literal["session", "keyring"]
    model: str = Field(default="", max_length=200)
    make_default: bool = False
    context_tokens: int = Field(default=8192, ge=2, le=2_000_000)
    output_tokens: int = Field(default=2048, ge=1, lt=2_000_000)
    requires_key: bool = False
    allowed_models: list[str] = Field(default_factory=list, max_length=50)

    def fields(self):
        return {**self.model_dump(exclude={"api_key"}),
                "api_key": self.api_key.get_secret_value() if self.api_key else None}


class ProfileUpdateInput(ProfileInput):
    clear_api_key: bool = False

    def fields(self):
        fields = super().fields()
        if "allowed_models" not in self.model_fields_set:
            fields.pop("allowed_models")
        return fields


class RoutingInput(ProviderInput):
    mode: Literal["local_only", "local_first", "balanced", "quality"]
    providers: list[str] = Field(max_length=20)


@router.get("")
def get_providers():
    return settings.status()


@router.post("/onboarding")
def dismiss_onboarding(body: OnboardingInput):
    return settings.dismiss(body.dismissed)


@router.post("/profiles")
def create_profile(body: ProfileInput):
    return settings.save_profile(**body.fields())


@router.post("/profiles/test")
async def test_unsaved_profile(body: ProfileInput):
    profile = await run_in_threadpool(settings.prepare_profile, **body.fields())
    connection = {key: profile[key] for key in ("kind", "base_url", "is_local", "context_tokens", "output_tokens", "requires_key", "allowed_models")}
    connection.update(id=None, api_key=body.api_key.get_secret_value() if body.api_key else None)
    return await provider_transport.discover(connection)


@router.put("/profiles/{provider_id}")
def update_profile(provider_id: str, body: ProfileUpdateInput):
    return settings.save_profile(provider_id=provider_id, **body.fields())


@router.get("/routing")
def get_routing():
    return settings.routing()


@router.put("/routing")
def save_routing(body: RoutingInput):
    return settings.save_routing(body.mode, body.providers)


@router.post("/{provider}/test")
async def test_provider(provider: str, body: TestInput):
    if provider not in providers.PROVIDERS:
        connection = await run_in_threadpool(settings._connection, provider, body.api_key.get_secret_value() if body.api_key else None)
        return await provider_transport.discover(connection)
    providers.validate_provider(provider)
    credential = body.api_key.get_secret_value() if body.api_key is not None else (await run_in_threadpool(settings.resolve, provider))[0]
    if not credential:
        raise HTTPException(422, "Provide an API key or configure this provider first.")
    return await providers.list_models(provider, credential)


@router.put("/{provider}")
def save_provider(provider: str, body: SaveInput):
    return settings.save(provider, body.api_key.get_secret_value() if body.api_key is not None else None, body.storage, body.model, body.make_default)


@router.delete("/{provider}")
def delete_provider(provider: str):
    return settings.remove(provider)
