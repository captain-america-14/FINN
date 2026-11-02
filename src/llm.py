import os
import re
import time

from dotenv import load_dotenv

from src.config import (
    BASE_DIR,
    LLM_MAX_OUTPUT_TOKENS,
    LLM_MIN_SECONDS_BETWEEN_CALLS,
    LLM_MODEL,
)

load_dotenv(BASE_DIR / ".env", override=True)

_last_call = 0.0


def model_name() -> str:
    """The model in use. GEMINI_MODEL in .env overrides config, so you can switch without editing code."""
    return os.environ.get("GEMINI_MODEL", "").strip() or LLM_MODEL


def llm_configured() -> bool:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    placeholder = (not key) or key.lower().startswith("your") or key.lower().endswith("here")
    return not placeholder


def _thinking_config(types, model: str):
    """Switch thinking off: it adds latency and tokens, and summarising needs no deep reasoning."""
    name = model.lower()
    try:
        if "gemini-3" in name:
            return types.ThinkingConfig(thinking_level="minimal" if "flash" in name else "low")
        if "flash" in name:
            return types.ThinkingConfig(thinking_budget=0)
    except Exception:
        return None
    return None


def _wait_for_slot() -> None:
    """Keep at least LLM_MIN_SECONDS_BETWEEN_CALLS between requests (free-tier rate limits)."""
    global _last_call
    wait = LLM_MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def call_llm(system: str, user: str, max_tokens: int = LLM_MAX_OUTPUT_TOKENS) -> dict:
    """One request to the model. Returns {text, input_tokens, output_tokens, stop_reason}.

    This is the ONLY place that knows which provider we use.
    """
    if not llm_configured():
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to the .env file in the project root.")
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError:
        raise RuntimeError("The 'google-genai' package is not installed (pip install google-genai).")

    model = model_name()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"].strip())
    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        thinking_config=_thinking_config(types, model),
    )

    response = None
    delay = 20
    for attempt in range(3):
        _wait_for_slot()
        try:
            response = client.models.generate_content(model=model, contents=user, config=config)
            break
        except errors.APIError as err:
            message = str(err)
            code = getattr(err, "code", None)
            # A per-day quota will not clear by waiting a minute, so only retry per-minute limits
            daily_quota = "perday" in re.sub(r"[^a-z]", "", message.lower())
            if code == 429 and attempt < 2 and not daily_quota:
                time.sleep(delay)
                delay *= 2
                continue
            if code == 429:
                raise RuntimeError(
                    "Gemini rate limit or daily free-tier quota reached. Wait and try again later. "
                    f"Details: {message[:300]}"
                ) from err
            raise RuntimeError(f"Gemini API error: {message[:500]}") from err

    if response is None:
        raise RuntimeError("Gemini returned no response.")

    try:
        text = (response.text or "").strip()
    except Exception:
        text = ""
    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    finish_name = getattr(finish, "name", str(finish)) if finish is not None else "none"
    if not text:
        block = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        raise RuntimeError(f"Gemini returned no text (finish reason: {finish_name}, block reason: {block}).")

    usage = getattr(response, "usage_metadata", None)
    return {
        "text": text,
        "input_tokens": getattr(usage, "prompt_token_count", 0) or 0,
        "output_tokens": getattr(usage, "candidates_token_count", 0) or 0,
        "stop_reason": finish_name,
    }


if __name__ == "__main__":
    # Live smoke test: confirms the key, the model ID and the request settings work
    reply = call_llm("Reply with exactly the JSON object requested and nothing else.", 'Return {"ok": true}')
    print("Model:", model_name())
    print("Reply:", reply["text"])
    print("Tokens in/out:", reply["input_tokens"], reply["output_tokens"], "| finish:", reply["stop_reason"])