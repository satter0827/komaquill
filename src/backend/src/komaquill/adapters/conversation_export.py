"""Write shareable copies while retaining original work and image metadata."""

import io
import json
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

from komaquill.conversations.models import Conversation
from komaquill.domain.content import ContentRating
from komaquill.domain.models import Run


def export_conversation(
    workspace: Path,
    conversation: Conversation,
    rating: ContentRating,
    assets: list[tuple[Run, list[Path]]],
    *,
    include_images: bool = True,
    include_conditions: bool = False,
) -> Path:
    destination = workspace / ".artifacts/data/exports" / f"{uuid4()}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    transcript = [conversation.definition.title]
    for turn in conversation.turns:
        transcript.extend(
            value
            for value in (turn.user, turn.narration, turn.action_narration, turn.assistant)
            if value
        )
    temporary = destination.with_suffix(".partial")
    try:
        _write_export(
            temporary,
            conversation,
            rating,
            assets,
            transcript,
            include_images=include_images,
            include_conditions=include_conditions,
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _write_export(
    destination: Path,
    conversation: Conversation,
    rating: ContentRating,
    assets: list[tuple[Run, list[Path]]],
    transcript: list[str],
    *,
    include_images: bool,
    include_conditions: bool,
) -> None:
    with ZipFile(destination, "x", compression=ZIP_DEFLATED) as archive:
        archive.writestr("conversation.txt", "\n\n".join(transcript))
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format_version": 1,
                    "conversation_id": str(conversation.id),
                    "rating": rating,
                    "include_images": include_images,
                    "include_conditions": include_conditions,
                },
                ensure_ascii=False,
            ),
        )
        for run, paths in assets:
            if include_conditions:
                archive.writestr(
                    f"conditions/{run.run_id}.json", run.model_dump_json(exclude_unset=True)
                )
            if include_images:
                for index, source in enumerate(paths):
                    with Image.open(source) as image:
                        output = io.BytesIO()
                        clean = image.copy()
                        clean.info.clear()
                        clean.save(output, format="PNG")
                    archive.writestr(f"images/{run.run_id}-{index}.png", output.getvalue())
