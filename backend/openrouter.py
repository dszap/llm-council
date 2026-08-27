"""OpenRouter API client for making LLM requests."""

import logging
import time
import httpx
from typing import List, Dict, Any, Optional
from .config import OPENROUTER_API_KEY, OPENROUTER_API_URL
from .logging_config import LoggingSettings, log_event


logger = logging.getLogger("llm_council.openrouter")


async def query_model(
    model: str,
    messages: List[Dict[str, str]],
    timeout: float = 120.0
) -> Optional[Dict[str, Any]]:
    """
    Query a single model via OpenRouter API.

    Args:
        model: OpenRouter model identifier (e.g., "openai/gpt-4o")
        messages: List of message dicts with 'role' and 'content'
        timeout: Request timeout in seconds

    Returns:
        Response dict with 'content' and optional 'reasoning_details', or None if failed
    """
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": messages,
    }
    settings = LoggingSettings.from_env()
    started_at = time.perf_counter()
    event_fields = {
        "model": model,
        "message_count": len(messages),
    }
    if settings.log_llm_payloads:
        event_fields["messages"] = messages
    log_event(
        logger,
        logging.INFO,
        "openrouter.request.started",
        "Model request started",
        **event_fields,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                OPENROUTER_API_URL,
                headers=headers,
                json=payload
            )
            response.raise_for_status()

            data = response.json()
            message = data['choices'][0]['message']
            content = message.get('content')
            completion_fields = {
                "model": model,
                "message_count": len(messages),
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "response_char_count": len(content) if isinstance(content, str) else 0,
            }
            if settings.log_llm_payloads:
                completion_fields["content"] = content
            log_event(
                logger,
                logging.INFO,
                "openrouter.request.completed",
                "Model request completed",
                **completion_fields,
            )

            return {
                'content': message.get('content'),
                'reasoning_details': message.get('reasoning_details')
            }

    except httpx.TimeoutException:
        error_category = "timeout"
        error_fields = {}
    except httpx.HTTPStatusError as error:
        error_category = "http_status"
        error_fields = {"status_code": error.response.status_code}
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        error_category = "malformed_response"
        error_fields = {}
    except Exception:
        error_category = "unexpected"
        error_fields = {}

    log_event(
        logger,
        logging.ERROR,
        "openrouter.request.failed",
        "Model request failed",
        model=model,
        message_count=len(messages),
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        error_category=error_category,
        **error_fields,
    )
    return None


async def query_models_parallel(
    models: List[str],
    messages: List[Dict[str, str]]
) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Query multiple models in parallel.

    Args:
        models: List of OpenRouter model identifiers
        messages: List of message dicts to send to each model

    Returns:
        Dict mapping model identifier to response dict (or None if failed)
    """
    import asyncio

    # Create tasks for all models
    tasks = [query_model(model, messages) for model in models]

    # Wait for all to complete
    responses = await asyncio.gather(*tasks)

    # Map models to their responses
    return {model: response for model, response in zip(models, responses)}
