"""
LLM Client - Gemini 2.5 Flash (primary) → Groq/Llama fallback on 429.
Usage:
    from src.llm_client import llm_complete
    text = llm_complete(prompt, max_tokens=400)
"""

import os
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

_GEMINI_MODEL = "gemini-2.0-flash"
_GROQ_MODEL   = "llama-3.3-70b-versatile"

_DATE_PREFIX = (
    "Today's date is {date}. All market data, news, and events in this conversation "
    "reflect the current real-world situation as of that date.\n\n"
)


def _inject_date(system: str) -> str:
    """Prepend today's date to the system prompt to anchor the model in time."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    return _DATE_PREFIX.format(date=date_str) + system if system else _DATE_PREFIX.format(date=date_str).strip()


def llm_complete(prompt: str, system: str = "", max_tokens: int = 500) -> str:
    """
    Try Gemini 2.5 Flash first. On 429 (rate limit) fall back to Groq.
    Returns response text, or raises on total failure.
    """
    system_with_date = _inject_date(system)

    result = _try_gemini(prompt, system_with_date, max_tokens)
    if result is not None:
        return result

    logger.warning("Gemini rate-limited — falling back to Groq")
    return _try_groq(prompt, system_with_date, max_tokens)


# ── Gemini ─────────────────────────────────────────────────────────────────────

def _try_gemini(prompt: str, system: str, max_tokens: int) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.debug("GEMINI_API_KEY not set — skipping")
        return None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        full_prompt = f"{system}\n\n{prompt}".strip() if system else prompt

        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=0.3,
            ),
        )
        text = response.text.strip().replace('\x00', '').replace('\u0000', '')
        text = ''.join(c for c in text if c >= ' ' or c in '\n\r\t')
        logger.debug(f"Gemini response: {len(text)} chars")
        return text

    except Exception as e:
        err = str(e)
        if "429" in err or "quota" in err.lower() or "rate" in err.lower() or "RESOURCE_EXHAUSTED" in err:
            logger.warning(f"Gemini 429/quota — falling back: {err}")
            return None
        logger.warning(f"Gemini error — falling back: {err}")
        return None


# ── Groq ───────────────────────────────────────────────────────────────────────

def _try_groq(prompt: str, system: str, max_tokens: int) -> str:
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set — no LLM available")
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.1,
        )
        text = resp.choices[0].message.content.strip()
        logger.debug(f"Groq fallback response: {len(text)} chars")
        return text
    except Exception as e:
        logger.error(f"[llm] Groq error: {e}")
        raise RuntimeError(f"Groq failed: {e}") from e
