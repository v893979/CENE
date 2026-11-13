"""Content screening of tool results (AI-firewall layer), feeding the deterministic taint rule.

A screener reads text an agent is about to see (e.g. a document or a data row with free-text
notes). A positive finding is treated as a *signal*, not a verdict: the gateway marks the session
as tainted, so later write actions in that session go to two-person escrow (or are denied, per the
data contract). The deterministic checks still decide every action.

  NoScreener          - screening off (default)
  MarkerScreener      - TESTS ONLY: flags text containing a neutral marker string, so scenarios can
                        simulate a positive classifier finding without containing any attack text
  ModelArmorScreener  - Google Cloud Model Armor, sanitizeUserPrompt on a template you create
                        (enable the prompt-injection/jailbreak and malicious-URL filters)

Model Armor REST call (see Google Cloud docs, "Sanitize prompts and responses"):
  POST https://modelarmor.<LOCATION>.rep.googleapis.com/v1/projects/<P>/locations/<LOCATION>/templates/<T>:sanitizeUserPrompt
  body {"userPromptData": {"text": "..."}}
  match  <=> response.sanitizationResult.filterMatchState == "MATCH_FOUND"
"""
from __future__ import annotations

TEST_MARKER = "[[CENE-TEST-FLAG]]"


class NoScreener:
    name = "none"

    def screen(self, text: str) -> dict:
        return {"flagged": False}


class MarkerScreener:
    name = "marker"

    def __init__(self, markers: list[str] | None = None):
        self.markers = markers or [TEST_MARKER]

    def screen(self, text: str) -> dict:
        hit = any(m in text for m in self.markers)
        return {"flagged": hit, "filters": ["test_marker"] if hit else []}


class ModelArmorScreener:
    name = "model_armor"

    def __init__(self, template: str, fail_flagged: bool = True, max_chars: int = 20000):
        """template = projects/<P>/locations/<LOCATION>/templates/<T>.

        fail_flagged=True: if the screening call fails, treat the content as flagged (fail closed -
        the only consequence is that later writes need two approvers)."""
        self.template = template
        self.location = template.split("/locations/")[1].split("/")[0]
        self.fail_flagged = fail_flagged
        self.max_chars = max_chars
        self._creds = None

    def _token(self) -> str:
        import google.auth
        import google.auth.transport.requests

        if self._creds is None:
            self._creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return self._creds.token

    def screen(self, text: str) -> dict:
        import requests

        url = (f"https://modelarmor.{self.location}.rep.googleapis.com/v1/{self.template}:sanitizeUserPrompt")
        try:
            r = requests.post(url, json={"userPromptData": {"text": text[: self.max_chars]}},
                              headers={"Authorization": f"Bearer {self._token()}"}, timeout=10)
            r.raise_for_status()
            return self.parse(r.json())
        except Exception as e:  # network, auth or quota problems
            return {"flagged": self.fail_flagged, "filters": [], "error": type(e).__name__}

    @staticmethod
    def parse(resp: dict) -> dict:
        res = resp.get("sanitizationResult", {})
        flagged = res.get("filterMatchState") == "MATCH_FOUND"
        filters = []
        for item in res.get("filterResults", {}).values() if isinstance(res.get("filterResults"), dict) else []:
            for name, val in (item or {}).items():
                if isinstance(val, dict) and val.get("matchState") == "MATCH_FOUND":
                    filters.append(name)
        return {"flagged": flagged, "filters": filters}


def make_screener(kind: str | None, template: str | None = None):
    if kind in (None, "", "none"):
        return NoScreener()
    if kind == "marker":
        return MarkerScreener()
    if kind == "model_armor":
        if not template:
            raise ValueError("CENE_MODEL_ARMOR_TEMPLATE is required for the model_armor screener")
        return ModelArmorScreener(template)
    raise ValueError(f"unknown screener '{kind}'")
