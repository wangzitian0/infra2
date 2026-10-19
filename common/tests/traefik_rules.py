"""Evaluate Traefik router rules and compose router labels against a request.

A test that matches substrings of a rule proves the text, not the routing. This module
parses the small rule language the compose files use and answers one question: which
router serves a given host and path. Unknown functions fail loudly, so a new matcher
cannot silently evaluate to false.

Supported grammar: ``Host``, ``Path`` and ``PathPrefix`` with one or more backtick
arguments, ``&&``, ``||``, ``!`` and parentheses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(
    r"\s*(?:(?P<func>[A-Za-z]+)\((?P<args>[^)]*)\)|(?P<op>&&|\|\||!|\(|\)))"
)
_ARG = re.compile(r"`([^`]*)`")


def _matcher(func: str, args: list[str], host: str, path: str) -> bool:
    if func == "Host":
        return host in args
    if func == "Path":
        return path in args
    if func == "PathPrefix":
        return any(path.startswith(prefix) for prefix in args)
    raise ValueError(f"unsupported Traefik matcher: {func}")


def rule_matches(rule: str, *, host: str, path: str) -> bool:
    """Return True when ``rule`` selects a request for ``host`` and ``path``."""
    tokens: list[tuple[str, object]] = []
    position = 0
    stripped = rule.strip()
    while position < len(stripped):
        match = _TOKEN.match(stripped, position)
        if match is None:
            raise ValueError(f"cannot parse Traefik rule at {stripped[position:]!r}")
        position = match.end()
        if match["func"]:
            tokens.append(("call", (match["func"], _ARG.findall(match["args"]))))
        else:
            tokens.append(("op", match["op"]))

    index = 0

    def peek() -> tuple[str, object] | None:
        return tokens[index] if index < len(tokens) else None

    def take() -> tuple[str, object]:
        nonlocal index
        token = tokens[index]
        index += 1
        return token

    def parse_or() -> bool:
        value = parse_and()
        while peek() == ("op", "||"):
            take()
            right = parse_and()
            value = value or right
        return value

    def parse_and() -> bool:
        value = parse_not()
        while peek() == ("op", "&&"):
            take()
            right = parse_not()
            value = value and right
        return value

    def parse_not() -> bool:
        if peek() == ("op", "!"):
            take()
            return not parse_not()
        return parse_atom()

    def parse_atom() -> bool:
        kind, payload = take()
        if kind == "op" and payload == "(":
            value = parse_or()
            if take() != ("op", ")"):
                raise ValueError("unbalanced parenthesis in Traefik rule")
            return value
        if kind == "call":
            func, args = payload  # type: ignore[misc]
            return _matcher(func, args, host, path)
        raise ValueError(f"unexpected token in Traefik rule: {payload!r}")

    result = parse_or()
    if index != len(tokens):
        raise ValueError("trailing tokens in Traefik rule")
    return result


@dataclass
class Router:
    name: str
    rule: str = ""
    priority: int | None = None
    middlewares: list[str] = field(default_factory=list)
    service: str = ""

    @property
    def effective_priority(self) -> int:
        # Traefik defaults the priority to the rule length when none is set.
        return self.priority if self.priority is not None else len(self.rule)


def routers_from_labels(labels: list[str]) -> dict[str, Router]:
    """Collect ``traefik.http.routers.<name>.<attr>`` labels into routers."""
    routers: dict[str, Router] = {}
    prefix = "traefik.http.routers."
    for label in labels:
        key, _, value = label.partition("=")
        if not key.startswith(prefix):
            continue
        name, _, attribute = key[len(prefix) :].partition(".")
        router = routers.setdefault(name, Router(name=name))
        if attribute == "rule":
            router.rule = value
        elif attribute == "priority":
            router.priority = int(value)
        elif attribute == "middlewares":
            router.middlewares = [item for item in value.split(",") if item]
        elif attribute == "service":
            router.service = value
    return routers


def serving_router(
    routers: dict[str, Router], *, host: str, path: str
) -> Router | None:
    """The router Traefik would pick: highest priority among the rules that match."""
    matching = [
        router
        for router in routers.values()
        if router.rule and rule_matches(router.rule, host=host, path=path)
    ]
    if not matching:
        return None
    return max(matching, key=lambda router: router.effective_priority)
