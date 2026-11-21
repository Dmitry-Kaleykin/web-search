"""Shared research contract and exceptional retrieval guidance."""

from importlib.resources import files

RESEARCH_WORKFLOW = files("web_research").joinpath("research_workflow.md").read_text()


def search_guidance(outcome):
    if outcome == "backend_unavailable":
        return [
            "Access failure is not evidence of absence. Read known URLs or use an accessible "
            "alternative; avoid immediately retrying blocked engines."
        ]
    if outcome == "empty":
        return ["No matches. Try a more specific query, another language, or a known source URL."]
    return []


def read_guidance(status):
    if status != "ok":
        return [
            "Inspect warnings before using this page. Try rendering, a visual, or another source "
            "if relevant text is missing."
        ]
    return []
