"""Turn any vendor response into canonical Quotes and QuestionnaireAnswers.

readers   file router -> text dumps with locators, plus images
agent     one extraction agent per response, batched, with validation and one retry
verify    snippet checks against the source text
normalize stated price -> RFx canonical unit, INR ex-tax
store     canonical rows, review items, response status
cache     raw model output on disk
"""

from core.extract.agent import DEFAULT_MODEL, ExtractionResult, extract_response
from core.extract.store import persist_extraction


def run_extraction(conn, response_id, folder, rfx, client, **kwargs):
    """Extract one response with the model, then store the canonical rows."""
    result = extract_response(client, folder, rfx, **kwargs)
    summary = persist_extraction(conn, response_id, rfx, result)
    return result, summary


__all__ = ["DEFAULT_MODEL", "ExtractionResult", "extract_response", "persist_extraction", "run_extraction"]
