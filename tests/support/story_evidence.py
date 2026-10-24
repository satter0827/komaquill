"""Summarize successful and rejected planning with the same evidence contract."""

from datetime import UTC, datetime


def planning_measurement(
    api, story_id, *, case_id, count, source_sha256, started_at, elapsed_seconds, failure=None
):
    evidence = api.stories.export(story_id) if story_id else None
    attempts = evidence["planning"]["attempts"] if evidence else []
    usages = [row["response"]["usage"] if row["response"] else None for row in attempts]
    total = (
        sum(row["total_tokens"] for row in usages)
        if all(row is not None and row["total_tokens"] is not None for row in usages)
        else None
    )
    plan = evidence["plan"] if evidence else None
    provenance = (
        (plan["planner"] if plan else evidence["planning"]["provenance"]) if evidence else None
    )
    return {
        "format_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": "failed" if failure else "saved",
        "failure": {"code": str(failure.code), "stage": str(failure.stage)} if failure else None,
        "identity": provenance.get("app_identity") if provenance else None,
        "case_id": case_id,
        "source_sha256": source_sha256,
        "requested_images": count,
        "actual_images": len(plan["scenes"]) if plan else None,
        "story_id": str(story_id) if story_id else None,
        "calls": len(attempts),
        "total_tokens": total,
        "elapsed_seconds": elapsed_seconds,
        "usage": usages,
        "evidence": evidence,
    }
