"""LLM call. One small provider-agnostic interface so switching models is a
config change (`llm.provider` / `llm.model` in `.poliac.yaml`), not a code change -- the
original goal was to compare recommendations across Claude, Gemini, ChatGPT, DeepSeek.

Gemini, Anthropic (Claude), OpenAI (ChatGPT), and DeepSeek are implemented; add another
provider by implementing `LlmClient` and registering it in `create_client`.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

from poliac.llm.schema import AdvisorResult


class LlmError(Exception):
    pass


class LlmClient(ABC):
    @abstractmethod
    def complete(self, system_instruction: str, prompt: str, temperature: float) -> AdvisorResult:
        """One structured-output call. Temperature 0 for determinism."""

    @abstractmethod
    def complete_with_correction(
        self,
        system_instruction: str,
        original_prompt: str,
        prior_response: AdvisorResult,
        errors: str,
        temperature: float,
    ) -> AdvisorResult:
        """The one retry policy allows: same conversation, plus the prior
        response and the specific validation errors, asking for a corrected full result."""


def _extract_advisor_result(response, provider: str) -> AdvisorResult:
    if isinstance(response.parsed, AdvisorResult):
        return response.parsed
    if response.parsed is not None:
        return AdvisorResult.model_validate(response.parsed)

    text = getattr(response, "text", None)
    if text:
        try:
            return AdvisorResult.model_validate_json(text)
        except Exception as e:
            raise LlmError(f"{provider} response did not match the schema: {text!r}") from e

    raise LlmError(f"{provider} returned no usable content")


class GeminiClient(LlmClient):
    def __init__(self, model: str, api_key: str | None = None) -> None:
        try:
            from google import genai
        except ImportError as e:
            raise LlmError(
                "the 'google-genai' package is required for provider=gemini "
                "(pip install 'poliac[gemini]')"
            ) from e

        api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise LlmError("GEMINI_API_KEY is not set")

        self._model = model
        self._client = genai.Client(api_key=api_key)

    def _generate(self, system_instruction: str, contents, temperature: float) -> AdvisorResult:
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=AdvisorResult,
                ),
            )
        except Exception as e:
            raise LlmError(f"Gemini API call failed: {e}") from e

        return _extract_advisor_result(response, "Gemini")

    def complete(self, system_instruction: str, prompt: str, temperature: float = 0) -> AdvisorResult:
        return self._generate(system_instruction, prompt, temperature)

    def complete_with_correction(
        self,
        system_instruction: str,
        original_prompt: str,
        prior_response: AdvisorResult,
        errors: str,
        temperature: float = 0,
    ) -> AdvisorResult:
        contents = [
            {"role": "user", "parts": [{"text": original_prompt}]},
            {"role": "model", "parts": [{"text": prior_response.model_dump_json()}]},
            {"role": "user", "parts": [{"text": errors}]},
        ]
        return self._generate(system_instruction, contents, temperature)


class AnthropicClient(LlmClient):
    """Claude, via forced tool use -- Anthropic has no `response_schema` mode like Gemini's, so
    the schema is given as a single tool the model is forced to call (`tool_choice`), and the
    structured result is its `input`."""

    _TOOL_NAME = "record_advisor_result"

    def __init__(self, model: str, api_key: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise LlmError(
                "the 'anthropic' package is required for provider=anthropic "
                "(pip install 'poliac[anthropic]')"
            ) from e

        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LlmError("ANTHROPIC_API_KEY is not set")

        self._model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    def _tool_schema(self) -> dict:
        return {
            "name": self._TOOL_NAME,
            "description": "Record the structured Terraform recommendation result.",
            "input_schema": AdvisorResult.model_json_schema(),
        }

    def _generate(self, system_instruction: str, messages: list[dict], temperature: float) -> AdvisorResult:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system_instruction,
                temperature=temperature,
                tools=[self._tool_schema()],
                tool_choice={"type": "tool", "name": self._TOOL_NAME},
                messages=messages,
            )
        except Exception as e:
            raise LlmError(f"Claude API call failed: {e}") from e

        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == self._TOOL_NAME:
                return AdvisorResult.model_validate(block.input)
        raise LlmError("Claude returned no usable tool_use content")

    def complete(self, system_instruction: str, prompt: str, temperature: float = 0) -> AdvisorResult:
        return self._generate(system_instruction, [{"role": "user", "content": prompt}], temperature)

    def complete_with_correction(
        self,
        system_instruction: str,
        original_prompt: str,
        prior_response: AdvisorResult,
        errors: str,
        temperature: float = 0,
    ) -> AdvisorResult:
        messages = [
            {"role": "user", "content": original_prompt},
            {"role": "assistant", "content": prior_response.model_dump_json()},
            {"role": "user", "content": errors},
        ]
        return self._generate(system_instruction, messages, temperature)


class OpenAiClient(LlmClient):
    """ChatGPT, via the structured-output `beta.chat.completions.parse` helper
    (`response_format=AdvisorResult`, enforced JSON schema, same spirit as Gemini's
    `response_schema`)."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LlmError(
                "the 'openai' package is required for provider=openai "
                "(pip install 'poliac[openai]')"
            ) from e

        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LlmError("OPENAI_API_KEY is not set")

        self._model = model
        self._client = OpenAI(api_key=api_key)

    def _generate(self, system_instruction: str, messages: list[dict], temperature: float) -> AdvisorResult:
        try:
            response = self._client.beta.chat.completions.parse(
                model=self._model,
                temperature=temperature,
                messages=[{"role": "system", "content": system_instruction}, *messages],
                response_format=AdvisorResult,
            )
        except Exception as e:
            raise LlmError(f"OpenAI API call failed: {e}") from e

        choice = response.choices[0].message
        if choice.parsed is not None:
            return choice.parsed
        if choice.refusal:
            raise LlmError(f"OpenAI refused: {choice.refusal}")
        raise LlmError("OpenAI returned no usable content")

    def complete(self, system_instruction: str, prompt: str, temperature: float = 0) -> AdvisorResult:
        return self._generate(system_instruction, [{"role": "user", "content": prompt}], temperature)

    def complete_with_correction(
        self,
        system_instruction: str,
        original_prompt: str,
        prior_response: AdvisorResult,
        errors: str,
        temperature: float = 0,
    ) -> AdvisorResult:
        messages = [
            {"role": "user", "content": original_prompt},
            {"role": "assistant", "content": prior_response.model_dump_json()},
            {"role": "user", "content": errors},
        ]
        return self._generate(system_instruction, messages, temperature)


class DeepSeekClient(LlmClient):
    """DeepSeek's API is OpenAI-compatible, so this reuses the `openai` SDK pointed at
    DeepSeek's base URL -- but DeepSeek has no `response_format=<pydantic model>` structured
    mode, only plain JSON mode (`response_format={"type": "json_object"}`), so the schema is
    given as an instruction in the system prompt and the response is parsed/validated by hand,
    same as Gemini's text fallback path (`_extract_advisor_result`)."""

    _BASE_URL = "https://api.deepseek.com"

    def __init__(self, model: str, api_key: str | None = None) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LlmError(
                "the 'openai' package is required for provider=deepseek "
                "(pip install 'poliac[deepseek]') -- DeepSeek's API is OpenAI-compatible"
            ) from e

        api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise LlmError("DEEPSEEK_API_KEY is not set")

        self._model = model
        self._client = OpenAI(api_key=api_key, base_url=self._BASE_URL)

    def _generate(self, system_instruction: str, messages: list[dict], temperature: float) -> AdvisorResult:
        schema_note = (
            "Respond with a single JSON object matching exactly this JSON schema, no prose "
            f"outside it:\n{AdvisorResult.model_json_schema()}"
        )
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": f"{system_instruction}\n\n{schema_note}"},
                    *messages,
                ],
            )
        except Exception as e:
            raise LlmError(f"DeepSeek API call failed: {e}") from e

        text = response.choices[0].message.content
        if not text:
            raise LlmError("DeepSeek returned no usable content")
        try:
            return AdvisorResult.model_validate_json(text)
        except Exception as e:
            raise LlmError(f"DeepSeek response did not match the schema: {text!r}") from e

    def complete(self, system_instruction: str, prompt: str, temperature: float = 0) -> AdvisorResult:
        return self._generate(system_instruction, [{"role": "user", "content": prompt}], temperature)

    def complete_with_correction(
        self,
        system_instruction: str,
        original_prompt: str,
        prior_response: AdvisorResult,
        errors: str,
        temperature: float = 0,
    ) -> AdvisorResult:
        messages = [
            {"role": "user", "content": original_prompt},
            {"role": "assistant", "content": prior_response.model_dump_json()},
            {"role": "user", "content": errors},
        ]
        return self._generate(system_instruction, messages, temperature)


_CLIENTS: dict[str, type[LlmClient]] = {
    "gemini": GeminiClient,
    "anthropic": AnthropicClient,
    "claude": AnthropicClient,
    "openai": OpenAiClient,
    "chatgpt": OpenAiClient,
    "deepseek": DeepSeekClient,
}


def create_client(provider: str, model: str) -> LlmClient:
    cls = _CLIENTS.get(provider)
    if cls is None:
        raise LlmError(f"unknown or not-yet-implemented llm provider: {provider!r}")
    return cls(model=model)
