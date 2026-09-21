from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel

from .progress import LoadingProgress

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class Location(BaseModel):
    institution_normalized: str
    status: Literal["resolved", "ambiguous", "unknown"]
    city: Optional[str]
    region: Optional[str]
    country: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    confidence: Literal["high", "medium", "low"]
    reason: str
    sources: List[str]


class ExtractedAffiliation(BaseModel):
    author_name: str
    institution: str
    raw_affiliation: str
    evidence: str


class Extraction(BaseModel):
    affiliations: List[ExtractedAffiliation]


class LLM:
    provider = "gemini-adc"

    def __init__(self, project, model, location="global"):
        if not project:
            raise ValueError("Set GOOGLE_CLOUD_PROJECT to your Google Cloud project ID in .env.")
        with LoadingProgress("Loading the Gemini SDK and ADC credentials"):
            import google.auth
            from google.auth.exceptions import DefaultCredentialsError
            from google import genai
            from google.genai import types
            try:
                credentials, _ = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"], quota_project_id=project)
            except DefaultCredentialsError as exc:
                raise ValueError("ADC credentials are unavailable. First run: bash scripts/setup_adc.sh " + project) from exc
            # Explicit credentials ensure that API-key environment variables cannot select key auth.
            self.client = genai.Client(
                vertexai=True, project=project, location=location, credentials=credentials,
                http_options=types.HttpOptions(api_version="v1", timeout=120_000,
                    retry_options=types.HttpRetryOptions(attempts=4)))
        self.model = model

    @staticmethod
    def _check_response(response):
        candidates = response.candidates or []
        if not candidates or candidates[0].finish_reason != "STOP" or not response.text:
            raise ValueError("Gemini returned an incomplete response (possibly blocked or truncated).")

    def parse(self, prompt, content, schema, web_search=False):
        import json
        from google.genai import types

        sources = set()
        search_id = ""
        if web_search:
            # Gemini 2.5 cannot combine Search grounding and JSON schema in one request.
            search = self.client.models.generate_content(
                model=self.model, contents=content,
                config=types.GenerateContentConfig(
                    system_instruction=prompt + "\nFor this step, return verification notes instead of JSON. Use Google Search to verify the city and its city-center coordinates.",
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
            self._check_response(search)
            metadata = search.candidates[0].grounding_metadata
            if metadata:
                for chunk in metadata.grounding_chunks or []:
                    if chunk.web and chunk.web.uri:
                        sources.add(chunk.web.uri)
            search_id = search.response_id or ""
            content = json.dumps({"input": content, "verification_notes": search.text,
                                  "allowed_source_urls": sorted(sources)}, ensure_ascii=False)
            prompt += "\nStructure the result using only the supplied verification notes. In sources, copy URLs exactly from allowed_source_urls. Return unknown if the notes provide no evidence."
        response = self.client.models.generate_content(
            model=self.model, contents=content,
            config=types.GenerateContentConfig(system_instruction=prompt,
                response_mime_type="application/json", response_schema=schema,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        self._check_response(response)
        parsed = response.parsed
        if not isinstance(parsed, schema):
            parsed = schema.model_validate_json(response.text)
        response_id = ";".join(filter(None, [search_id, response.response_id]))
        return parsed.model_dump(), response_id, sources


def prompt(name):
    return (PROMPTS / name).read_text(encoding="utf-8")
