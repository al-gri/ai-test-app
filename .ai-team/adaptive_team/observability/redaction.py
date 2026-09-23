"""Bounded structured log sanitization BEFORE serialization or persistence.

Heuristics cannot recognize arbitrary encrypted/encoded secrets. Register known
credential values explicitly, log metadata by default, and never treat this as
an authorization to persist complete private prompts or provider payloads.
"""
import math
import re
from urllib.parse import quote
from ..models import PolicyError

SENSITIVE = re.compile(r"(?i)(api[-_]?key|token|secret|password|passwd|authorization|cookie|private[-_]?key|credential)")
PATTERNS = [
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"(?i)[\"']?(?:api[-_]?key|(?:access[-_]?|refresh[-_]?)?token|secret|password|authorization)[\"']?\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s&,;]+)"),
    re.compile(r"(?i)(?:https?|postgres(?:ql)?|mysql)://[^/@\s]+:[^/@\s]+@"),
]


class Redactor:
    def __init__(self, known_secrets=(), *, max_chars=32000):
        if type(max_chars) is not int or not 256 <= max_chars <= 1_000_000:
            raise PolicyError("Invalid redaction output bound")
        if not isinstance(known_secrets, (list, tuple)) or len(known_secrets) > 1000:
            raise PolicyError("Known secrets require a bounded list")
        values = set()
        for secret in known_secrets:
            if not isinstance(secret, str) or not 1 <= len(secret) <= 16000:
                raise PolicyError("Invalid registered secret")
            values.update((secret, quote(secret, safe='')))
        self.secrets, self.max_chars = tuple(sorted(values, key=len, reverse=True)), max_chars

    def text(self, value):
        # Oversized text is dropped in full: truncating first could preserve a
        # partial secret and defeat the exact-value matcher at the boundary.
        if len(value) > self.max_chars:
            return '***'
        for secret in self.secrets:
            value = value.replace(secret, '***')
        for pattern in PATTERNS:
            value = pattern.sub('***', value)
        return value

    def clean(self, value):
        budget = [0]
        def visit(item, depth):
            budget[0] += 1
            if depth > 16 or budget[0] > 2000:
                return '***'
            if isinstance(item, str):
                return self.text(item)
            if item is None or type(item) in (bool, int):
                return item
            if type(item) is float:
                return item if math.isfinite(item) else '***'
            if isinstance(item, dict):
                result = {}
                for key, child in list(item.items())[:200]:
                    if not isinstance(key, str):
                        continue
                    safe_key = self.text(key)
                    result[safe_key] = '***' if SENSITIVE.search(key) else visit(child, depth + 1)
                return result
            if isinstance(item, (list, tuple)):
                return [visit(child, depth + 1) for child in item[:200]]
            return '***'  # Never invoke repr/str on arbitrary objects.
        return visit(value, 0)
