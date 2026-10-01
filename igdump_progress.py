"""Plain-text progress bars that also work in saved console logs."""


def format_progress(stage: str, current: int, total: int | None = None, detail: str = "") -> str:
    if total is None or total <= 0:
        counter = f"[...] {current}"
    else:
        current = max(0, min(current, total))
        width = 24
        filled = width * current // total
        counter = f"[{'#' * filled}{'-' * (width - filled)}] {current}/{total} ({100 * current // total}%)"
    return f"{stage} {counter}" + (f" — {detail}" if detail else "")
