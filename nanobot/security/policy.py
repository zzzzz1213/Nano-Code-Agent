from __future__ import annotations

from typing import Any, Dict, List, Optional


class PolicyEngine:
    """Simple policy engine PoC.

    Rules are evaluated in order. Each rule is a dict:
      {"action": "allow"|"block"|"require_confirmation",
       "match": {"meta.key": value, ...},
       "reason": "..."}

    Matching currently supports equality against values in `doc["meta"]`.
    """

    def __init__(self, rules: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rules: List[Dict[str, Any]] = rules or []

    def set_rules(self, rules: List[Dict[str, Any]]) -> None:
        self.rules = rules or []

    def get_rules(self) -> List[Dict[str, Any]]:
        return self.rules

    def _get_meta_value(self, doc: Dict[str, Any], key: str) -> Any:
        """Support simple dot path for meta lookups, e.g. 'meta.safety'."""
        parts = key.split(".")
        cur: Any = doc
        for p in parts:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    def evaluate(self, doc: Dict[str, Any], session_key: Optional[str] = None) -> Dict[str, Any]:
        """Evaluate rules against a document.

        Returns: {"decision": "allow"|"block"|"require_confirmation", "rule": matched_rule_or_None, "reason": str}
        """
        meta = doc.get("meta", {}) if isinstance(doc, dict) else {}
        for rule in self.rules:
            match = rule.get("match") or {}
            ok = True
            for k, v in match.items():
                val = self._get_meta_value({"meta": meta}, k) if k.startswith("meta.") else self._get_meta_value(doc, k)
                if val != v:
                    ok = False
                    break
            if ok:
                return {"decision": rule.get("action", "block"), "rule": rule, "reason": rule.get("reason")}
        return {"decision": "allow", "rule": None, "reason": "no rule matched"}
