"""Local provider credentials and non-secret, atomically persisted preferences."""

from contextlib import contextmanager
import asyncio
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

import httpx

try:
    import keyring
except ImportError:
    keyring = None


PROVIDERS = {
    "openai": ("OpenAI", "OPENAI_API_KEY", "https://api.openai.com/v1/models"),
    "anthropic": ("Anthropic", "ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/models"),
}
MODEL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}"
KEYRING_SERVICE = "self_mcp.providers"
MAX_MODEL_PAGES = 5
MODEL_LIST_TIMEOUT = 30.0
PROFILE_PATTERN = r"profile_[0-9a-f]{32}"
PROFILE_KINDS = {"ollama", "openai_compatible"}
ROUTING_MODES = {"local_only", "local_first", "balanced", "quality"}


def normalize_base_url(value: str) -> tuple[str, bool]:
    """Validate explicit connection configuration without making network requests."""
    try:
        if not isinstance(value, str) or not 1 <= len(value) <= 2048:
            raise ValueError
        decoded = unquote(value)
        if any(not c.isprintable() or c.isspace() for c in decoded) or "\\" in decoded:
            raise ValueError
        url = urlsplit(value)
        host = url.hostname
        if url.scheme not in {"http", "https"} or not host or url.username is not None or url.password is not None:
            raise ValueError
        if "?" in value or "#" in value or "%" in host or (url.port is not None and not 1 <= url.port <= 65535):
            raise ValueError
        local = host in {"localhost", "127.0.0.1", "::1"}
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError
            if all(part.isdigit() or part.startswith("0x") for part in host.split(".")):
                raise ValueError
            if host in {"metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal"}:
                raise ValueError
        else:
            checked = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
            metadata = str(checked) in {"100.100.100.200", "168.63.129.16", "fd00:ec2::254", "fd20:ce::254"}
            if metadata or checked.is_link_local or checked.is_unspecified or checked.is_multicast or (checked.is_reserved and not local):
                raise ValueError
        if url.scheme == "http" and not local:
            raise ValueError
        # Reject ambiguous path traversal/encoded separators rather than changing hosts or paths implicitly.
        if any(part in {".", ".."} for part in decoded.split("/")) or re.search(r"%2f|%5c|%25", value, re.I):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if url.port is not None and url.port != (443 if url.scheme == "https" else 80):
            authority += f":{url.port}"
        path = re.sub(r"/+", "/", url.path).rstrip("/")
        return urlunsplit((url.scheme, authority, path, "", "")), local
    except (ValueError, TypeError, UnicodeError):
        raise ProviderError(422, "Provide an HTTP loopback or HTTPS provider base URL without credentials, query, fragment, or unsafe addresses.") from None


def validate_metadata(values, secrets) -> None:
    for value in values:
        decoded = unquote(value)
        if any(secret and (secret in value or secret in decoded) for secret in secrets):
            raise ProviderError(422, "Provider metadata must not contain API credentials.")


def validate_allowed_models(value) -> list[str]:
    if not isinstance(value, list) or len(value) > 50 or any(
        not isinstance(model, str) or not re.fullmatch(MODEL_PATTERN, model) for model in value
    ) or len(set(value)) != len(value):
        raise ProviderError(422, "Allowed models must contain at most 50 unique, valid model identifiers.")
    return list(value)


def validate_profile(name, kind, base_url, model, context_tokens, output_tokens, requires_key, storage, allowed_models=None):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or len(name) > 80 or not name.isprintable():
        raise ProviderError(422, "Provide a profile name of 1-80 printable characters.")
    if kind not in PROFILE_KINDS or storage not in {"session", "keyring"} or type(requires_key) is not bool:
        raise ProviderError(422, "Invalid provider profile settings.")
    if not isinstance(model, str) or (model and not re.fullmatch(MODEL_PATTERN, model)):
        raise ProviderError(422, "Provide a valid model identifier, or leave it empty until discovery.")
    if type(context_tokens) is not int or type(output_tokens) is not int or not 2 <= context_tokens <= 2_000_000 or not 1 <= output_tokens < context_tokens:
        raise ProviderError(422, "Token limits must satisfy 1 <= output_tokens < context_tokens <= 2000000.")
    base_url, local = normalize_base_url(base_url)
    allowed_models = validate_allowed_models([] if allowed_models is None else allowed_models)
    return {"name": name.strip(), "kind": kind, "base_url": base_url, "model": model,
            "context_tokens": context_tokens, "output_tokens": output_tokens,
            "requires_key": requires_key, "is_local": local, "allowed_models": allowed_models}


class ProviderError(Exception):
    def __init__(self, status: int, message: str, *, kind=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.kind = kind


def validate_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ProviderError(404, "Unknown provider. Choose openai or anthropic.")


def limit_error_message(provider: str, raw: bytes, retry_after: str | None = None) -> str:
    """Classify allowlisted error codes without exposing upstream text or identifiers."""
    validate_provider(provider)
    name = PROVIDERS[provider][0]
    codes = set()
    try:
        payload = json.loads(raw) if len(raw) <= 1024 * 1024 else None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            codes = {value for key in ("code", "type") if isinstance(value := error.get(key), str)}
    except (ValueError, TypeError):
        pass

    quota_codes = {"insufficient_quota", "billing_hard_limit_reached", "billing_not_active"}
    if provider == "openai" and codes.intersection(quota_codes):
        code = sorted(codes.intersection(quota_codes))[0]
        return (
            f"{name} API quota/billing blocked this request ({code}). "
            "Check API credits, billing, and project/organization spending limits for this key. "
            "A ChatGPT subscription does not include API credits. Waiting or increasing this app's task limits will not fix a billing block."
        )

    rate_code = "rate_limit_exceeded" if provider == "openai" else "rate_limit_error"
    if rate_code in codes:
        wait = "Wait before retrying"
        if retry_after and re.fullmatch(r"[0-9]{1,5}", retry_after) and 0 < int(retry_after) <= 86400:
            wait = f"The provider asks you to wait at least {int(retry_after)} seconds before retrying"
        return (
            f"{name} API request/token rate limit reached ({rate_code}). {wait}, "
            "then check limits for the selected model and API project. Other applications may share those limits. "
            "No automatic retry was made; this is not the app's task or revision limit."
        )

    return (
        f"{name} returned HTTP 429 without a recognized reason. "
        "Check both API billing/quota and request/token rate limits for this key and model. "
        "The provider response did not distinguish them; this is not the app's task or revision limit."
    )


class ProviderSettings:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else Path.home() / ".self_mcp/provider_settings.json"
        self._sessions: dict[str, str] = {}
        self._mutex = threading.RLock()

    @contextmanager
    def _locked(self):
        # A separate lock inode remains stable when os.replace swaps the JSON file.
        with self._mutex:
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
                with os.fdopen(fd, "a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                raise ProviderError(503, "Provider preferences are unavailable.") from None

    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not isinstance(raw, dict):
                raise ValueError
            models = raw.get("models", {})
            if not isinstance(models, dict):
                raise ValueError
            profiles = raw.get("profiles", {})
            if not isinstance(profiles, dict) or len(profiles) > 18:
                raise ValueError
            clean_profiles = {}
            for identifier, profile in profiles.items():
                if not re.fullmatch(PROFILE_PATTERN, identifier) or not isinstance(profile, dict):
                    raise ValueError
                storage = profile.get("credential_storage", "none")
                if storage not in {"none", "session", "keyring"}:
                    raise ValueError
                clean = validate_profile(
                    profile.get("name"), profile.get("kind"), profile.get("base_url"),
                    profile.get("model", ""), profile.get("context_tokens", 8192),
                    profile.get("output_tokens", 2048), profile.get("requires_key", False),
                    storage if storage != "none" else "session",
                    validate_allowed_models(profile.get("allowed_models", [])),
                )
                clean["credential_storage"] = storage
                # An expired session credential must not downgrade to anonymous access.
                clean["requires_key"] = clean["requires_key"] or storage != "none"
                clean_profiles[identifier] = clean
            known = set(PROVIDERS) | set(clean_profiles)
            routing = raw.get("routing", {"mode": "local_only", "providers": []})
            self._validate_routing(routing, known)
            # Only these fields may ever be written back, even after external edits.
            return {
                "models": {
                    p: m for p, m in models.items()
                    if p in PROVIDERS and isinstance(m, str) and re.fullmatch(MODEL_PATTERN, m)
                },
                "default_provider": raw.get("default_provider")
                if raw.get("default_provider") in known else None,
                "onboarding_dismissed": raw.get("onboarding_dismissed") is True,
                "profiles": clean_profiles,
                "routing": {"mode": routing["mode"], "providers": list(routing["providers"])},
            }
        except (ValueError, TypeError, ProviderError):
            raise ProviderError(503, "Provider preferences could not be read.") from None

    def _write(self, settings: dict) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as output:
                temporary = output.name
                json.dump(settings, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _backend():
        if keyring is None:
            return None
        try:
            backend = keyring.get_keyring()
            # Never use alternate file backends or chains that could fall back to one.
            if backend.priority <= 0 or type(backend).__module__.startswith(
                ("keyrings.alt", "keyring.backends.chainer")
            ):
                return None
            return backend
        except Exception:
            return None

    def _resolve(self, provider: str, backend) -> tuple[str | None, str | None]:
        env = os.environ.get(PROVIDERS[provider][1], "").strip()
        if env:
            return env, "environment"
        if provider in self._sessions:
            return self._sessions[provider], "session"
        if backend is not None:
            try:
                credential = backend.get_password(KEYRING_SERVICE, provider)
            except Exception:
                raise ProviderError(503, "System keyring is unavailable or locked.") from None
            if credential:
                return credential, "keyring"
        return None, None

    def _profile_resolve(self, provider, profile):
        storage = profile["credential_storage"]
        if storage == "session":
            credential = self._sessions.get(provider)
            return credential, "session" if credential else "none"
        if storage == "keyring":
            backend = self._backend()
            if backend is None:
                return None, "none"
            try:
                credential = backend.get_password(KEYRING_SERVICE, provider)
            except Exception:
                raise ProviderError(503, "System keyring is unavailable or locked.") from None
            return credential, "keyring" if credential else "none"
        return None, "none"

    @staticmethod
    def _profile(settings, provider):
        if provider not in settings["profiles"]:
            raise ProviderError(404, "Unknown provider profile.")
        return settings["profiles"][provider]

    def resolve(self, provider: str) -> tuple[str | None, str | None]:
        if provider in PROVIDERS:
            with self._mutex:
                return self._resolve(provider, self._backend())
        with self._locked():
            return self._profile_resolve(provider, self._profile(self._load(), provider))

    def connection(self, provider_id: str) -> dict:
        return self._connection(provider_id)

    def _connection(self, provider_id: str, api_key: str | None = None) -> dict:
        """Resolve a snapshot under one lock; test keys never change saved state."""
        with self._locked():
            settings = self._load()
            if provider_id in PROVIDERS:
                credential = api_key or self._resolve(provider_id, self._backend())[0]
                result = {"id": provider_id, "kind": provider_id,
                          "base_url": PROVIDERS[provider_id][2].removesuffix("/models"),
                          "is_local": False, "api_key": credential, "context_tokens": 8192,
                          "output_tokens": 2048, "requires_key": True, "allowed_models": []}
            else:
                profile = self._profile(settings, provider_id)
                credential = api_key or self._profile_resolve(provider_id, profile)[0]
                validate_metadata((profile["name"], profile["model"], profile["base_url"], *profile["allowed_models"]), [credential])
                result = {key: profile[key] for key in ("kind", "base_url", "is_local", "context_tokens", "output_tokens", "requires_key", "allowed_models")}
                result.update(id=provider_id, api_key=credential)
            if result["requires_key"] and not credential:
                raise ProviderError(422, "This provider requires an API key. Re-enter its credential before connecting.")
            return result

    @staticmethod
    def _validate_routing(routing, known):
        if not isinstance(routing, dict) or routing.get("mode") not in ROUTING_MODES:
            raise ProviderError(422, "Invalid routing mode.")
        ids = routing.get("providers")
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(p, str) or p not in known for p in ids) or len(set(ids)) != len(ids):
            raise ProviderError(422, "Routing requires at most 20 unique, existing provider IDs.")

    def routing(self) -> dict:
        with self._locked():
            return self._load()["routing"]

    def save_routing(self, mode: str, providers: list[str]) -> dict:
        with self._locked():
            settings = self._load()
            routing = {"mode": mode, "providers": list(providers)}
            self._validate_routing(routing, set(PROVIDERS) | set(settings["profiles"]))
            settings["routing"] = routing
            self._write(settings)
            return routing

    def catalog(self) -> list[dict]:
        """Read routing metadata without accessing credentials, environment, or keyring."""
        with self._locked():
            settings = self._load()
            rows = [{"id": provider, "name": name, "kind": provider,
                     "base_url": endpoint.removesuffix("/models"), "is_local": False,
                     "default_model": settings["models"].get(provider), "requires_key": True,
                     "context_tokens": 8192, "output_tokens": 2048, "allowed_models": []}
                    for provider, (name, _, endpoint) in PROVIDERS.items()]
            for provider, profile in settings["profiles"].items():
                rows.append({"id": provider, "default_model": profile["model"],
                             **{key: profile[key] for key in ("name", "kind", "base_url", "is_local",
                                                             "requires_key", "context_tokens", "output_tokens", "allowed_models")}})
            return rows

    def status(self) -> dict:
        with self._locked():
            settings = self._load()
            backend = self._backend()
            available = backend is not None
            providers = []
            for provider, (name, _, _) in PROVIDERS.items():
                try:
                    credential, source = self._resolve(provider, backend)
                except ProviderError:
                    credential, source = None, None
                    available = False
                providers.append({
                    "id": provider, "name": name, "configured": bool(credential),
                    "source": source, "default_model": settings["models"].get(provider),
                    "kind": provider, "base_url": PROVIDERS[provider][2].removesuffix("/models"),
                    "is_local": False, "allow_routing": provider in settings["routing"]["providers"],
                    "allowed_models": [],
                })
            for provider, profile in settings["profiles"].items():
                try:
                    credential, source = self._profile_resolve(provider, profile)
                except ProviderError:
                    credential, source = None, "none"
                    available = False
                validate_metadata((profile["name"], profile["model"], profile["base_url"], *profile["allowed_models"]), [credential])
                providers.append({
                    "id": provider, "name": profile["name"],
                    "configured": not profile["requires_key"] or bool(credential),
                    "source": source, "default_model": profile["model"],
                    **{key: profile[key] for key in ("kind", "base_url", "is_local", "context_tokens", "output_tokens", "requires_key", "allowed_models")},
                    "allow_routing": provider in settings["routing"]["providers"],
                })
            return {
                "providers": providers, "keyring_available": available,
                "default_provider": settings["default_provider"],
                "onboarding_dismissed": settings["onboarding_dismissed"],
                "routing": settings["routing"],
            }

    @contextmanager
    def _keyring_change(self, provider: str, backend, credential: str | None):
        if backend is None:
            yield
            return
        try:
            previous = backend.get_password(KEYRING_SERVICE, provider)
        except Exception:
            raise ProviderError(503, "Could not read the system keyring. No changes were saved; unlock it and retry.") from None
        # Rollback credentials stay in memory only, never in a JSON journal.
        changed = previous != credential
        try:
            if changed:
                if credential is None:
                    backend.delete_password(KEYRING_SERVICE, provider)
                else:
                    backend.set_password(KEYRING_SERVICE, provider, credential)
            yield
        except Exception:
            if changed:
                try:
                    if previous is not None:
                        backend.set_password(KEYRING_SERVICE, provider, previous)
                    elif backend.get_password(KEYRING_SERVICE, provider) is not None:
                        backend.delete_password(KEYRING_SERVICE, provider)
                except Exception:
                    raise ProviderError(503, "Provider update failed and keyring rollback failed. Preferences and session credentials were not changed, but system keyring state is uncertain. Inspect the OS credential store and retry.") from None
            raise ProviderError(503, "Provider update failed. Previous credentials and preferences were preserved. Check access to the system keyring and preferences file, then retry.") from None

    def save(self, provider: str, api_key: str | None, storage: str, model: str, make_default: bool) -> dict:
        validate_provider(provider)
        with self._locked():
            settings = self._load()
            backend = self._backend()
            if api_key is not None and os.environ.get(PROVIDERS[provider][1], "").strip():
                raise ProviderError(409, f"{PROVIDERS[provider][1]} is set and takes precedence. Unset it before saving a key, or omit api_key to update settings.")
            credential, _ = (api_key, None) if api_key is not None else self._resolve(provider, backend)
            if not credential:
                raise ProviderError(422, "Configure a provider credential before saving settings.")
            if model and (not re.fullmatch(MODEL_PATTERN, model) or credential in model):
                raise ProviderError(422, "Provide a valid, non-secret model identifier (1-200 characters).")
            if model:
                settings["models"][provider] = model
            else:
                settings["models"].pop(provider, None)
            if make_default:
                settings["default_provider"] = provider
            if api_key is not None:
                if storage == "keyring" and backend is None:
                    raise ProviderError(503, "A secure system keyring is unavailable. Session storage cannot guarantee removal of an older saved key; use an environment variable or restore keyring access. No plaintext fallback is supported.")
                with self._keyring_change(provider, backend, api_key if storage == "keyring" else None):
                    self._write(settings)
                # Commit process state only after the non-secret preferences commit.
                if storage == "keyring":
                    self._sessions.pop(provider, None)
                else:
                    self._sessions[provider] = api_key
            else:
                self._write(settings)
        message = "Provider settings saved. Session credentials last for this API process only and are shared by all local clients. No inference was performed."
        if api_key is not None and storage == "session" and backend is None:
            message += " Warning: keyring is unavailable, so any previously saved keyring credential could not be removed. This is not a confirmed session-only switch: the older credential may become active again after restart when keyring access returns. Restore keyring access and save the session key again to remove it."
        return {"message": message}

    def prepare_profile(self, *, name: str, kind: str, base_url: str, storage: str,
                        model: str = "", api_key: str | None = None, make_default: bool = False,
                        context_tokens: int = 8192, output_tokens: int = 2048,
                        requires_key: bool = False, allowed_models: list[str] | None = None) -> dict:
        if api_key and not re.fullmatch(r"[!-~]{1,4096}", api_key):
            raise ProviderError(422, "Invalid API key format.")
        profile = validate_profile(name, kind, base_url, model, context_tokens, output_tokens, requires_key, storage, allowed_models)
        with self._mutex:
            secrets = [api_key, *self._sessions.values(), *(os.environ.get(p[1]) for p in PROVIDERS.values())]
            validate_metadata((name, model, base_url, *profile["allowed_models"]), secrets)
        profile["requires_key"] = requires_key or bool(api_key)
        return profile

    def save_profile(self, *, provider_id: str | None = None, clear_api_key: bool = False,
                     api_key: str | None = None, storage: str, make_default: bool = False, **fields) -> dict:
        api_key = api_key or None
        if clear_api_key and api_key:
            raise ProviderError(422, "Choose either a new API key or clear_api_key.")
        profile = self.prepare_profile(api_key=api_key, storage=storage, **fields)
        with self._locked():
            settings = self._load()
            previous = self._profile(settings, provider_id) if provider_id is not None else None
            if previous is None and len(settings["profiles"]) >= 18:
                raise ProviderError(422, "At most 18 custom provider profiles are supported.")
            if previous and any(profile[key] != previous[key] for key in ("kind", "base_url")):
                raise ProviderError(422, "Profile kind and base URL are immutable. Recreate the profile to use a different endpoint.")
            previous_storage = previous["credential_storage"] if previous else "none"
            if previous and "allowed_models" not in fields:
                profile["allowed_models"] = list(previous["allowed_models"])
            old_key = self._profile_resolve(provider_id, previous)[0] if previous else None
            validate_metadata((profile["name"], profile["model"], profile["base_url"], *profile["allowed_models"]), [old_key, api_key])
            provider_id = provider_id or "profile_" + uuid4().hex
            profile["credential_storage"] = storage if api_key else ("none" if clear_api_key else previous_storage)
            if previous and not clear_api_key:
                profile["requires_key"] = profile["requires_key"] or previous["requires_key"]
            settings["profiles"][provider_id] = profile
            if make_default:
                settings["default_provider"] = provider_id
            change_vault = (api_key and storage == "keyring") or ((api_key or clear_api_key) and previous_storage == "keyring")
            backend = self._backend() if change_vault else None
            if change_vault and backend is None:
                raise ProviderError(503, "A secure system keyring is unavailable. Restore keyring access before changing its credentials.")
            with self._keyring_change(provider_id, backend, api_key if storage == "keyring" and not clear_api_key else None):
                self._write(settings)
            if api_key or clear_api_key:
                self._sessions.pop(provider_id, None)
                if api_key and storage == "session":
                    self._sessions[provider_id] = api_key
            return {"id": provider_id, "message": "Provider profile saved. Session credentials last for this API process only. No inference was performed."}

    def remove(self, provider: str) -> dict:
        with self._locked():
            settings = self._load()
            if provider not in PROVIDERS:
                profile = self._profile(settings, provider)
                backend = self._backend() if profile["credential_storage"] == "keyring" else None
                if profile["credential_storage"] == "keyring" and backend is None:
                    raise ProviderError(503, "Restore system keyring access before removing this profile and its credential.")
                del settings["profiles"][provider]
            else:
                backend = self._backend()
            settings["models"].pop(provider, None)
            settings["routing"]["providers"] = [p for p in settings["routing"]["providers"] if p != provider]
            if settings["default_provider"] == provider:
                settings["default_provider"] = None
            with self._keyring_change(provider, backend, None):
                self._write(settings)
            self._sessions.pop(provider, None)
            if provider not in PROVIDERS:
                return {"message": "Provider profile, managed credentials, and routing membership removed."}
            message = "App-managed session credential and model settings removed."
            if backend is None:
                message += " Keyring unavailable; any existing keyring credential could not be removed."
            else:
                message += " App-managed keyring credential removed."
            message += " Environment variables cannot be deleted by this API; unset them in the API process environment and restart to remove environment credentials."
            return {"message": message}

    def dismiss(self, dismissed: bool) -> dict:
        with self._locked():
            settings = self._load()
            settings["onboarding_dismissed"] = dismissed
            self._write(settings)
        return {"onboarding_dismissed": dismissed}


async def list_models(provider: str, api_key: str) -> dict:
    validate_provider(provider)
    headers = {"Authorization": f"Bearer {api_key}"} if provider == "openai" else {
        "x-api-key": api_key, "anthropic-version": "2023-06-01",
    }
    models = {}
    cursor = None
    truncated = False
    try:
        # No redirects or environment proxies: credentials only reach the fixed endpoint.
        async with asyncio.timeout(MODEL_LIST_TIMEOUT), httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False, trust_env=False,
        ) as client:
            for page in range(MAX_MODEL_PAGES):
                params = {"limit": 100} if provider == "anthropic" else {}
                if cursor:
                    params["after_id"] = cursor
                response = await client.get(PROVIDERS[provider][2], headers=headers, params=params)
                if response.status_code in (401, 403):
                    raise ProviderError(401, "Provider authentication failed or model-list access was denied. Check the credential and its permissions.")
                if response.status_code == 429:
                    raise ProviderError(429, limit_error_message(provider, response.content, response.headers.get("retry-after")))
                if response.status_code != 200:
                    raise ProviderError(502, "Provider model listing is currently unavailable.")
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError
                page_limit = 100 if provider == "anthropic" else 1000
                for item in payload["data"][:page_limit]:
                    if not isinstance(item, dict):
                        raise ValueError
                    model_id = item.get("id")
                    if not isinstance(model_id, str) or not re.fullmatch(MODEL_PATTERN, model_id) or api_key in model_id:
                        continue
                    name = item.get("display_name", model_id)
                    if not isinstance(name, str) or not 1 <= len(name) <= 200 or not name.isprintable() or api_key in name:
                        name = model_id
                    models[model_id] = {"id": model_id, "name": name}
                truncated = truncated or len(payload["data"]) > page_limit
                if provider != "anthropic" or not payload.get("has_more"):
                    break
                next_cursor = payload.get("last_id")
                if not isinstance(next_cursor, str) or not re.fullmatch(MODEL_PATTERN, next_cursor) or next_cursor == cursor or api_key in next_cursor:
                    raise ValueError
                cursor = next_cursor
                if page == MAX_MODEL_PAGES - 1:
                    truncated = True
    except (httpx.TimeoutException, TimeoutError):
        raise ProviderError(504, "Provider model listing timed out. Try again later.") from None
    except httpx.RequestError:
        raise ProviderError(502, "Could not connect to the provider. Check your network and try again.") from None
    except (ValueError, TypeError, UnicodeError):
        raise ProviderError(502, "Provider returned an invalid model-list response.") from None
    message = "Authentication and model listing succeeded. This does not verify inference access or availability for any model."
    if truncated:
        message += " Model listing was truncated at the safety limit."
    return {"models": list(models.values()), "message": message}
