"""Summarize successful and rejected planning with the same evidence contract."""

from datetime import UTC, datetime

CASES = {
    "rainy-cat": None,
    "lost-key": "家を出る前、玲は机の上で鍵を探した。\n\n"
    "玲は机の引き出しを開け、小さな封筒を見つけた。\n\n"
    "封筒を開くと、中から鍵が出てきた。\n\n"
    "玲は鍵をポケットに入れ、安心して玄関へ向かった。",
    "shared-umbrella": "雨の日、駅の軒下で妹の澪が兄の航を待っていた。\n\n"
    "航が傘を差してやって来た。澪は自分の傘を忘れたと話した。\n\n"
    "航は傘を澪へ渡し、自分は駅で雨が止むのを待つことにした。\n\n"
    "澪は傘を差して家へ向かい、玄関で兄へのお礼の手紙を書いた。",
}


def planning_measurement(
    api, story_id, *, case_id, count, source_sha256, started_at, elapsed_seconds, failure=None
):
    exported = api.stories.export(story_id) if story_id else None
    evidence = exported.model_dump(mode="json") if exported else None
    attempts = evidence["planning"]["attempts"] if evidence else []
    usages = [row["usage"] for row in attempts]
    total = evidence["planning"]["usage"]["total_tokens"] if evidence else None
    plan = evidence["story"] if evidence else None
    return {
        "format_version": 1,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": "failed" if failure else "saved",
        "failure": {"code": str(failure.code), "stage": str(failure.stage)} if failure else None,
        "identity": exported.planning.producer if exported else None,
        "case_id": case_id,
        "source_sha256": source_sha256,
        "requested_images": count,
        "actual_images": plan["image_count"] if plan else None,
        "story_id": str(story_id) if story_id else None,
        "calls": len(attempts),
        "total_tokens": total,
        "elapsed_seconds": elapsed_seconds,
        "usage": usages,
        "evidence": evidence,
    }
