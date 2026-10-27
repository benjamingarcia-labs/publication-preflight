"""Pure, dependency-free text and Markdown preflight checks."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit

MAX_TEXT = 1024 * 1024
URL = re.compile(r"(?i)\b(?:https?://|file://|obsidian://)[^\s<>\]\)\"']+")
WIKILINK = re.compile(r"!?\[\[[^\]\n]+\]\]")
RAW_SCRIPT = re.compile(r"(?i)<\s*(?:script|iframe|object|embed)\b")
SENSITIVE_ASSIGNMENT = re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password)\s*[:=]\s*['\"]?[^\s'\"]{8,}")
PRIVATE_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home")


def _finding(line: int, code: str, description: str, severity: str = "block") -> dict:
    return {"line": line, "code": code, "message": description, "severity": severity}


def _url_issues(value: str) -> list[str]:
    value = unquote(value)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return ["Malformed URL"]
    if parsed.scheme in ("file", "obsidian"):
        return ["Local/private URI"]
    issues = ["Unencrypted HTTP URL"] if parsed.scheme == "http" else []
    if not host:
        return issues + ["URL has no valid hostname"]
    if host == "localhost" or host.endswith(PRIVATE_SUFFIXES):
        issues.append("Local hostname")
    try:
        if not ipaddress.ip_address(host).is_global:
            issues.append("Nonpublic IP address")
    except ValueError:
        pass
    if parsed.username or parsed.password:
        issues.append("Credentials embedded in URL")
    return issues


def scan(content: str, rules: list[dict]) -> list[dict]:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_TEXT:
        raise ValueError("Scan requires UTF-8 text no larger than 1 MiB")
    findings = []
    for lineno, line in enumerate(content.splitlines(), 1):
        lowered = line.casefold()
        for rule in rules:
            kind, value = rule["type"], rule["value"].casefold()
            if kind == "phrase" and value in lowered:
                findings.append(_finding(lineno, "custom-phrase", f"Protected phrase: {rule['name']}"))
            elif kind == "domain":
                for match in URL.finditer(line):
                    try:
                        host = (urlsplit(match.group()).hostname or "").casefold().rstrip(".")
                    except ValueError:
                        continue
                    if host == value or host.endswith("." + value):
                        findings.append(_finding(lineno, "custom-domain", f"Blocked domain: {rule['name']}"))
                        break
        for match in URL.finditer(line):
            for issue in _url_issues(match.group()):
                findings.append(_finding(lineno, "unsafe-url", issue))
        if WIKILINK.search(line):
            findings.append(_finding(lineno, "unresolved-link", "Unresolved wiki link/embed"))
        if RAW_SCRIPT.search(line):
            findings.append(_finding(lineno, "raw-embed", "Potentially unsafe embedded HTML"))
        if SENSITIVE_ASSIGNMENT.search(line):
            findings.append(_finding(lineno, "possible-secret", "Possible credential assignment; inspect manually"))
    return findings
