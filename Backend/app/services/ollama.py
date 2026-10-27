from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger("PAANGBA.ollama")

EXTRACT_SCHEMA = {
    "name": None,
    "age": None,
    "gender": None,
    "state": None,
    "district": None,
    "occupation": None,
    "occupation_type": None,
    # The model reports whichever period the person actually mentioned —
    # "20000 a month" -> monthly_income, "2.5 lakh a year" -> annual_income
    # — and validate_extraction() below deterministically converts
    # monthly_income to annual_income (x12) rather than trusting the model
    # to do that arithmetic itself.
    "monthly_income": None,
    "annual_income": None,
    "category": None,
    "is_entrepreneur": None,
    "has_existing_business": None,
    "has_land": None,
    "citizenship": "IN",
    "needs": [],
    "summary": "",
}

ALLOWED_OCCUPATION = {
    "farmer",
    "entrepreneur",
    "self_employed",
    "student",
    "salaried",
    "unemployed",
    "small_business",
    "micro_enterprise",
}
ALLOWED_CATEGORY = {"general", "sc", "st", "obc", "ews", "pwd", "minority"}
ALLOWED_GENDER = {"female", "male", "other"}


def _extract_json_blob(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _to_nonneg_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = float(value.strip().replace(",", ""))
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _resolve_annual_income(annual: Any, monthly: Any) -> int | None:
    """Annual income takes priority when both are present (e.g. the person
    stated both at different points and the yearly figure is the more
    precise/deliberate one). Otherwise a monthly figure is converted to a
    yearly one — deterministically, in Python — rather than trusting the
    model to do that multiplication itself."""
    resolved = _to_nonneg_number(annual)
    if resolved is not None:
        return int(resolved)
    resolved_monthly = _to_nonneg_number(monthly)
    if resolved_monthly is not None:
        return int(resolved_monthly * 12)
    return None


def validate_extraction(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(EXTRACT_SCHEMA)
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("name"), str):
        out["name"] = data["name"].strip()[:200] or None
    age = data.get("age")
    if isinstance(age, int) and 0 < age < 120:
        out["age"] = age
    elif isinstance(age, str) and age.isdigit():
        value = int(age)
        if 0 < value < 120:
            out["age"] = value
    gender = str(data.get("gender") or "").strip().lower()
    out["gender"] = gender if gender in ALLOWED_GENDER else None
    for field in ("state", "district", "occupation"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            out[field] = value.strip()[:120]
    occ = str(data.get("occupation_type") or "").strip().lower().replace(" ", "_").replace("-", "_")
    out["occupation_type"] = occ if occ in ALLOWED_OCCUPATION else None
    out["annual_income"] = _resolve_annual_income(data.get("annual_income"), data.get("monthly_income"))
    cat = str(data.get("category") or "").strip().lower()
    out["category"] = cat if cat in ALLOWED_CATEGORY else None
    for flag in ("is_entrepreneur", "has_existing_business", "has_land"):
        if isinstance(data.get(flag), bool):
            out[flag] = data[flag]
    cit = str(data.get("citizenship") or "IN").upper()
    out["citizenship"] = "IN" if cit in {"IN", "INDIA", "INDIAN"} else None
    needs = data.get("needs")
    if isinstance(needs, list):
        out["needs"] = [str(item)[:80] for item in needs[:12]]
    if isinstance(data.get("summary"), str):
        out["summary"] = data["summary"][:500]
    return out


class OllamaService:
    def __init__(self) -> None:
        self.available = False
        self.last_error: str | None = None
        # Model name we've confirmed is actually pulled in this Ollama
        # instance. Resolved lazily (and re-resolved if it ever stops
        # working) so a misconfigured/renamed OLLAMA_MODEL never hardcodes
        # a request to a model that doesn't exist — see _resolve_model().
        self._resolved_model: str | None = None

    def _list_models(self) -> list[str]:
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=2.0)
        response.raise_for_status()
        return [item.get("name", "") for item in response.json().get("models", [])]

    def _resolve_model(self) -> str | None:
        """Return a model name that is actually pulled in the local Ollama,
        preferring settings.ollama_model when it (or its base name, ignoring
        the ":tag" suffix) is available. Falls back to whatever model IS
        pulled rather than blindly sending requests for a hardcoded model
        that may not exist. Returns None only when Ollama itself is
        unreachable or has no models pulled at all."""
        if self._resolved_model:
            return self._resolved_model
        try:
            models = self._list_models()
        except Exception as exc:
            self.available = False
            self.last_error = str(exc)
            logger.warning("Ollama unreachable (url=%s): %s", settings.ollama_base_url, exc)
            return None

        self.available = True
        configured = settings.ollama_model
        for name in models:
            if name == configured or name.split(":")[0] == configured:
                self._resolved_model = name
                self.last_error = None
                return name

        if models:
            fallback = models[0]
            self.last_error = (
                f"Configured OLLAMA_MODEL '{configured}' is not pulled. "
                f"Falling back to '{fallback}'. Available models: {models}. "
                f"Run: ollama pull {configured}"
            )
            logger.warning(self.last_error)
            self._resolved_model = fallback
            return fallback

        self.last_error = f"No models are pulled in Ollama at {settings.ollama_base_url}. Run: ollama pull {configured}"
        logger.warning(self.last_error)
        return None

    def ping(self) -> bool:
        return self._resolve_model() is not None

    @property
    def resolved_model(self) -> str | None:
        """The model name actually last used for a successful request/ping,
        for diagnostics (see /api/health). None if never resolved yet."""
        return self._resolved_model

    def generate_json(self, prompt: str, language: str = "en") -> dict[str, Any] | None:
        model = self._resolve_model()
        if model is None:
            # Ollama is down or has no models — fail fast without attempting
            # a request that we already know will 404/refuse. Callers treat
            # None as "no AI available" and fall back to deterministic text.
            return None
        try:
            response = httpx.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0.1},
                },
                timeout=settings.ollama_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
            raw_text = body.get("response") or ""
            parsed = _extract_json_blob(raw_text)
            self.available = True
            if parsed is None:
                self.last_error = f"Ollama responded but no JSON object was found in: {raw_text[:200]!r}"
                logger.warning("Ollama JSON parse failed: %s", self.last_error)
            else:
                self.last_error = None
            return parsed
        except httpx.HTTPStatusError as exc:
            # A 404 here most often means the resolved model was deleted or
            # renamed after we cached it (e.g. `ollama rm` / `ollama pull` of
            # a different tag mid-session). Drop the cache so the next call
            # re-resolves against whatever is actually pulled now.
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning("Ollama model '%s' returned 404 — clearing cached model and will re-resolve.", model)
                self._resolved_model = None
            self.available = False
            self.last_error = str(exc)
            logger.warning("Ollama request failed (model=%s, url=%s): %s", model, settings.ollama_base_url, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - Ollama being down/slow/
            # malformed must never bubble up as a 500 to /api/dashboard or
            # any other caller; always degrade to "no AI available".
            self.available = False
            self.last_error = str(exc)
            logger.warning("Ollama request failed (model=%s, url=%s): %s", model, settings.ollama_base_url, exc)
            return None

    def extract_profile(self, text: str, language: str = "en") -> dict[str, Any] | None:
        prompt = (
            f"You extract structured citizen profile fields from multilingual text. "
            f"Respond only with JSON. User language code: {language}.\n"
            f"Schema keys: {list(EXTRACT_SCHEMA)}. occupation_type one of {sorted(ALLOWED_OCCUPATION)}. "
            f"category one of {sorted(ALLOWED_CATEGORY)}. gender one of {sorted(ALLOWED_GENDER)}. "
            f"citizenship is IN if the person is Indian.\n"
            f"Income: if the person mentions income, figure out from the wording whether it is a "
            f"MONTHLY amount (e.g. 'a month', 'per month', 'monthly', 'har mahine') or a YEARLY "
            f"amount (e.g. 'a year', 'per annum', 'yearly', 'annual', 'saalana'). Put a monthly "
            f"figure under 'monthly_income' and a yearly figure under 'annual_income' — report the "
            f"plain number only, do not multiply or convert it yourself. If no period is stated, "
            f"assume it is a yearly amount and use 'annual_income'. Never fill both.\n"
            f"Text:\n{text}"
        )
        parsed = self.generate_json(prompt, language)
        return validate_extraction(parsed) if parsed else None

    def explain_match(self, payload: dict[str, Any], language: str = "en") -> str | None:
        prompt = (
            f"Write a short citizen-friendly explanation in language {language}. "
            f"Do not mention Z3 or SAT. JSON context:\n{json.dumps(payload)[:2500]}"
        )
        parsed = self.generate_json(
            prompt + ' Return JSON {"explanation": "..."}',
            language,
        )
        if parsed and isinstance(parsed.get("explanation"), str):
            return parsed["explanation"][:600]
        return None


ollama_service = OllamaService()
