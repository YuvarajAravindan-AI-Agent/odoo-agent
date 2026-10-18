"""OPA policy decision point client. Fails closed: if OPA cannot answer, the answer is no."""
import requests

UNAVAILABLE = {"code": "policy_unavailable",
               "msg": "The policy engine could not be reached, so nothing can be proposed or executed."}


class PolicyDenied(Exception):
    """Raised by a tool's preview when the policy says no; no proposal is created."""
    def __init__(self, decision: dict):
        super().__init__("blocked by policy")
        self.decision = decision


class Policy:
    def __init__(self, url: str, timeout: float = 5.0):
        self.url, self.timeout = url, timeout

    def decide(self, facts: dict) -> dict:
        try:
            r = requests.post(self.url, json={"input": facts}, timeout=self.timeout)
            r.raise_for_status()
            body = r.json()
            d = body.get("result")
            if not isinstance(d, dict) or not isinstance(d.get("allow"), bool):
                raise ValueError("no decision in policy response")
        except Exception:  # noqa: BLE001  - any failure is a deny
            return denied_unavailable()
        d["decision_id"] = body.get("decision_id")
        return d


def denied_unavailable() -> dict:
    return {"allow": False, "decision_id": None, "risk": "high", "count": 0, "total": 0,
            "counts": {"policy_unavailable": 1}, "violations": [UNAVAILABLE]}
