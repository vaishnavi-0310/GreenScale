"""Manual live check for the runtime predictor.

Run from 7project/greenscale:

    python scripts/try_llm.py

Without GEMINI_API_KEY in .env every row reports ``fallback_no_key``.
With a key, rows should report ``llm``; a ``fallback_llm_error`` row means the
request failed or returned an unusable value, which is also a valid outcome to
document. Never paste real keys into this script or commit its output with
secrets.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.predictor import FALLBACK, predict_runtime  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

INPUT_SIZE_MB = 10


def main() -> None:
    print(f"key loaded: {bool(settings.gemini_api_key)}")
    print(f"model:      {settings.gemini_model}")
    print()
    print(f"{'workload_type':<14}{'input_mb':>9}{'seconds':>9}  source")
    for workload_type in FALLBACK:
        seconds, source = predict_runtime(workload_type, INPUT_SIZE_MB)
        print(f"{workload_type:<14}{INPUT_SIZE_MB:>9}{seconds:>9}  {source}")


if __name__ == "__main__":
    main()
