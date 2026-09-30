"""Result envelope shared by every woof_* tool (design section 5.2).

Text results are JSON strings. Image results use Hermes' multimodal form with
the envelope as the first text part and the MEDIA lines in text_summary.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

TEXT_CAP = 12_000

ERROR_KINDS = ("not_installed", "no_gpu", "refused", "invalid", "busy",
               "not_found", "engine_failed", "timeout")


def _drop_null_errors(obj: Any) -> Any:
    """Remove "error": null entries. Hermes marks any tool result whose text contains the key
    "error" as a failed call (shown as [error], counted by its repeated-failure guard), so a
    successful status that merely said "error": null looked like a failure."""
    if isinstance(obj, dict):
        return {k: _drop_null_errors(v) for k, v in obj.items() if not (k == "error" and v is None)}
    if isinstance(obj, list):
        return [_drop_null_errors(v) for v in obj]
    return obj


def _dump(obj: Mapping[str, Any]) -> str:
    return json.dumps(_drop_null_errors(obj), ensure_ascii=False, sort_keys=False, default=str)


def _capped(env: dict) -> str:
    """Serialize, trimming the data block until the text fits the cap."""
    text = _dump(env)
    if len(text) <= TEXT_CAP:
        return text
    data = env.get("data")
    if isinstance(data, dict):
        trimmed = dict(data)
        # Drop the largest fields first, keeping the scalar summary fields.
        for key in sorted(trimmed, key=lambda k: -len(_dump({k: trimmed[k]}))):
            if len(_dump({**env, "data": trimmed})) <= TEXT_CAP - 200:
                break
            value = trimmed[key]
            if isinstance(value, (list, dict, str)) and len(_dump({key: value})) > 400:
                if isinstance(value, list):
                    keep = list(value)
                    while keep and len(_dump({key: keep})) > 2000:
                        keep = keep[: max(1, len(keep) // 2)] if len(keep) > 1 else []
                    trimmed[key] = keep
                elif isinstance(value, str):
                    trimmed[key] = value[:1500] + "..."
                else:
                    trimmed[key] = {"omitted": "too large for one result; ask for it on its own"}
        trimmed["truncated"] = True
        text = _dump({**env, "data": trimmed})
    if len(text) > TEXT_CAP:
        text = _dump({"ok": env.get("ok"), "tool": env.get("tool"),
                      "summary": env.get("summary", ""),
                      "data": {"truncated": True,
                               "note": "result exceeded the size cap; narrow the request"}})
    return text


def ok(tool: str, summary: str, data: Mapping[str, Any] | None = None,
       next_tools: Sequence[str] = (), warnings: Iterable[str] = ()) -> str:
    return _capped(ok_dict(tool, summary, data, next_tools, warnings))


def ok_dict(tool: str, summary: str, data: Mapping[str, Any] | None = None,
            next_tools: Sequence[str] = (), warnings: Iterable[str] = ()) -> dict:
    return {"ok": True, "tool": tool, "summary": summary,
            "data": dict(data or {}), "next": list(next_tools),
            "warnings": [str(w) for w in warnings if w]}


def err(tool: str, kind: str, message: str, remedy: str = "") -> str:
    if kind not in ERROR_KINDS:
        kind = "engine_failed"
    return _dump({"ok": False, "tool": tool,
                  "error": {"kind": kind, "message": str(message), "remedy": str(remedy)}})


BLIND_NOTE = ("The pictures are not visible to you in this result, so do not say what they show; tell the user "
              "they are on screen, give the run's facts from the data, then copy the MEDIA lines into your reply.")


def _blind(summary: str) -> str:
    for ask in ("Describe what you see, then copy the MEDIA lines into your reply.",
                "Describe the differences, then copy the MEDIA lines into your reply."):
        if ask in summary:
            return summary.replace(ask, BLIND_NOTE)
    return summary


def multimodal(envelope: dict, images: Sequence[Path], media: Sequence[str],
               meta: Mapping[str, Any] | None = None, send_images: bool = True) -> dict | str:
    """Build the multimodal result. ``images`` go to the model, ``media`` are
    the absolute paths the user should see (MEDIA: lines)."""
    summary = envelope.get("summary", "")
    # text_summary is what the model gets when the host drops the images (a model the host does
    # not mark as vision-capable, or context compaction). It must not ask the model to describe
    # pictures it cannot see: that asked for invented weather.
    blind = _blind(summary)
    lines = [blind] + [f"MEDIA:{p}" for p in media]
    text_summary = "\n".join(lines)
    if not send_images or not images:
        # Plain text result; the MEDIA lines still reach the model so it can
        # copy them into its reply.
        env_text = _capped(dict(envelope, summary=blind))
        return env_text + "\n" + "\n".join(f"MEDIA:{p}" for p in media) if media else env_text
    env_text = _capped(envelope)
    content: list[dict] = [{"type": "text", "text": env_text + "\n" + "\n".join(f"MEDIA:{p}" for p in media)}]
    for path in images:
        raw = Path(path).read_bytes()
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}})
    return {"_multimodal": True, "content": content, "text_summary": text_summary,
            "meta": dict(meta or {})}
