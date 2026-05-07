# KPI Sheet Sync Reference

## Quick Checklist

1. Resolve Google auth from CLI args or environment variables only. If neither is provided, fail with an actionable auth error.
2. Resolve KPI auth from a cookie string, cookie file, or session JSON.
3. Find the target ad group in KPI.
4. Use the requested date preset. Default to `近30天`.
5. Extract text asset rows and their `Performance` from the KPI JSON endpoint.
6. In the worksheet, find rows whose status matches the requested marker such as `进行中`, `待确认`, or `优化中`.
7. Open the matching Google Sheet worksheet.
8. Update only the cells for fields that actually exist in the current worksheet.
9. Read back the updated cells.

## Matching Rules

- Match rows by exact asset text whenever possible.
- If exact text does not match, do not guess.
- Do not decide targets from optimization round labels. Use only the selected worksheet and the requested status marker.

## Cost Ranking

- Follow the worksheet's existing notation.
- If the row was previously labeled with an original rank and KPI now shows a new rank, write `old_rank>new_rank`.
- Keep the format consistent across the sheet.

## Data Period

- If the worksheet contains a `数据周期` field, follow the worksheet's existing notation.
- If the target cell is empty, write the exact KPI date range used for this sync.
- If the target cell uses append notation such as `26/3/20-`, append the update date of this run, for example `4/20`, rather than the KPI range end date.
- Prefer exact dates instead of relative phrases when writing a full range.
- If the worksheet does not contain a `数据周期` field, skip it without failing the worksheet.

## Best/Good Rate Formula

- Use text assets only.
- Formula:
  `Best/Good率 = (count of text assets with Performance in {Best, Good}) / (count of all current text assets in the ad group)`
- Example:
  If 3 of 10 text assets are `Best` or `Good`, write `30%`.

## Common Pitfalls

- Counting video rows in `Best/Good率`.
- Updating the wrong worksheet when several sheets have similar names.
- Writing guessed matches for assets with slightly different text.
- Implicitly relying on one machine's local cookie or credentials path instead of explicit auth input.
