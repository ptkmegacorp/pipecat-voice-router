#!/usr/bin/env python3
"""Shared voice command routing: normalize, strip fillers, exact + fuzzy match."""

import json
import os
from difflib import SequenceMatcher
from pathlib import Path

CONFIG = json.loads((Path(__file__).with_name("router_config.json")).read_text())

FUZZY_THRESHOLD = float(os.environ.get("VOICE_ROUTER_FUZZY_THRESHOLD", "85"))

FILLER_PREFIXES = (
    "uh ",
    "um ",
    "ah ",
    "er ",
    "hmm ",
    "hm ",
    "please ",
    "can you ",
    "could you ",
    "would you ",
    "hey ",
    "ok ",
    "okay ",
)

OPTIONAL_WORDS = frozenset({"that", "the", "a", "my", "this", "just"})


def norm(s: str) -> str:
    return " ".join(s.strip().lower().replace("-", " ").split())


def strip_fillers(text: str) -> str:
    t = text
    while True:
        matched = False
        for prefix in FILLER_PREFIXES:
            if t.startswith(prefix):
                t = t[len(prefix) :]
                matched = True
                break
        if not matched:
            break
    return t.strip()


def soften(text: str) -> str:
    return " ".join(w for w in text.split() if w not in OPTIONAL_WORDS)


def prepare_route_text(text: str) -> str:
    return soften(strip_fillers(norm(text)))


def fuzzy_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if a in b or b in a:
        return 95.0
    return SequenceMatcher(None, a, b).ratio() * 100.0


def _route_match(route: dict, prepared: str) -> tuple[bool, float, str | None]:
    if "match" not in route:
        return False, 0.0, None
    best_score = 0.0
    best_phrase = None
    for phrase in route["match"]:
        candidate = prepare_route_text(phrase)
        if prepared == candidate:
            return True, 100.0, phrase
        score = fuzzy_score(prepared, candidate)
        if score > best_score:
            best_score = score
            best_phrase = phrase
    if best_score >= FUZZY_THRESHOLD:
        return True, best_score, best_phrase
    return False, best_score, best_phrase


def _route_prefix(route: dict, prepared: str, raw: str) -> dict | None:
    if "prefix" not in route:
        return None
    prefix = prepare_route_text(route["prefix"])
    if not prepared.startswith(prefix):
        return None
    if "contains" in route and prepare_route_text(route["contains"]) not in prepared:
        return None
    args = dict(route.get("args", {}))
    remainder = prepared[len(prefix) :].strip()
    if route.get("query_arg"):
        args[route["query_arg"]] = remainder
    elif route.get("function") == "ask_pig":
        args["prompt"] = raw
    else:
        args["prompt"] = raw
    return {**route, "args": args}


def route_text(text: str, context: dict | None = None) -> dict:
    raw = text.strip()
    prepared = prepare_route_text(raw)
    ctx = context if context is not None else {}

    for route in CONFIG["routes"]:
        matched, score, phrase = _route_match(route, prepared)
        if matched:
            action = {**route, "text": raw, "context": ctx}
            if phrase and score < 100.0:
                action["match_method"] = "fuzzy"
                action["match_score"] = round(score, 1)
                action["match_phrase"] = phrase
            return action

        prefix_action = _route_prefix(route, prepared, raw)
        if prefix_action:
            return {**prefix_action, "text": raw, "context": ctx}

    fallback = CONFIG["fallback"]
    return {**fallback, "args": {"prompt": raw}, "text": raw, "context": ctx}
