# Bulk Sync Reference

## When To Use Bulk Mode

Use bulk mode when the user asks to sync all ad-group sheets in one Google Sheet instead of naming a single ad group.

## Safe Target Selection

1. Enumerate worksheet names.
2. Keep only visible worksheet tabs whose names look like ad group ids, for example `172801962239-en`.
3. If the user explicitly provides ad group ids, process only the matching worksheets.

## Safe Update Rule

- Update only the rows whose status exactly matches the requested marker. The marker is always required and must be confirmed by the user before each run; do not fall back to a hardcoded default.
- If there are no rows with the requested marker, skip copy-row updates for that worksheet and explain the reason in Chinese.
- Update only fields that can be found safely in the worksheet. Missing fields should be skipped, not treated as worksheet failure.
- Do not assume every worksheet uses the same round label or column positions.

## Performance Tips

- Prefer KPI `index-ajax` JSON responses over UI scraping for bulk sync.
- Reuse cookie strings, cookie files, or session JSON across the whole run.
- Run a dry-run before apply.
- Batch updates per worksheet rather than cell-by-cell writes.
- Prefer explicit auth parameters or environment variables over machine-specific local paths.

## Common Failure Modes

- Matching by round label instead of the requested status marker.
- Assuming `Performance` and `Cost排序` are always in fixed columns.
- Counting video assets in `Best/Good率`.
- Treating a missing optional field as a worksheet-level failure.
