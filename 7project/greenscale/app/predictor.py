"""Runtime prediction for batch workloads.

``predict_runtime`` asks Gemini for a rough duration estimate based ONLY on
job metadata (workload type and input size). It never sends API keys, raw
customer data or job descriptions to the model.

The LLM is advisory. Whatever happens (no key, network failure, quota, bad
JSON, absurd value) the function returns a bounded fallback so the scheduler
always has a number to work with. The second element of the returned tuple
records where the estimate came from so it can be audited later against the
actual measured runtime.
"""

import logging

from google.genai import types
from pydantic import BaseModel, Field

from app import llm
from app.config import settings

logger = logging.getLogger(__name__)

# Conservative per-workload defaults in seconds, used whenever the LLM cannot
# or should not be consulted. These are deliberately pessimistic.
FALLBACK: dict[str, int] = {"sleep": 60, "factorial": 90, "csv_summary": 120}

MIN_SECONDS = 1
MAX_SECONDS = 14_400  # 4 hours; anything beyond this is treated as nonsense

SOURCE_LLM = "llm"
SOURCE_NO_KEY = "fallback_no_key"
SOURCE_LLM_ERROR = "fallback_llm_error"

SYSTEM_PROMPT = (
    "You estimate how many seconds a small educational Kubernetes batch job "
    "will take to run. You only see metadata, so the estimate is uncertain; "
    "give a single realistic integer. Workload types: 'sleep' idles for a "
    "while, 'factorial' computes large factorials, 'csv_summary' aggregates a "
    "CSV file. Larger input_size_mb generally means longer runtime. "
    f"Answer must be between {MIN_SECONDS} and {MAX_SECONDS} seconds."
)


class RuntimeEstimate(BaseModel):
    """Structured response schema requested from Gemini."""

    estimated_seconds: int = Field(description="Estimated runtime in whole seconds")


def build_prompt(workload_type: str, input_size_mb: int) -> str:
    """Metadata-only prompt. Keep this tiny and free of secrets."""
    return f"workload_type={workload_type}; input_size_mb={input_size_mb}"


def predict_runtime(
    workload_type: str,
    input_size_mb: int,
    client=None,
) -> tuple[int, str]:
    """Return ``(estimated_seconds, source)``.

    ``source`` is one of ``"llm"``, ``"fallback_no_key"`` or
    ``"fallback_llm_error"``. Raises ``ValueError`` for an unknown workload
    type, because that indicates a programming error rather than an LLM one.

    ``client`` is injectable so tests can run without network access.
    """
    try:
        default = FALLBACK[workload_type]
    except KeyError:
        raise ValueError(f"Unknown workload_type: {workload_type!r}") from None

    if client is None and not settings.use_vertex_ai and not settings.gemini_api_key:
        return default, SOURCE_NO_KEY

    try:
        client = client or llm._get_client()
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=RuntimeEstimate,
            temperature=0,
        )
        raw = llm._generate_with_retry(
            client, build_prompt(workload_type, input_size_mb), config
        )
        seconds = RuntimeEstimate.model_validate_json(raw).estimated_seconds
        if not MIN_SECONDS <= seconds <= MAX_SECONDS:
            raise ValueError(f"Estimated runtime {seconds}s outside safe limits")
        return seconds, SOURCE_LLM
    except Exception as exc:  # noqa: BLE001 - any failure must degrade to fallback
        # Log only the exception type: never the prompt, response or key.
        logger.warning(
            "runtime prediction for %s fell back to %ss (%s)",
            workload_type,
            default,
            type(exc).__name__,
        )
        return default, SOURCE_LLM_ERROR
