# Next Steps

Planned improvements and known gaps, grouped by area.

---

## Task Cleanup / Clearing Mechanism

There is currently no way to delete or archive tasks in bulk. The following approaches are planned:

- **`/clear` command** — Telegram command to delete tasks by type, status, or age.
  Examples: `/clear queries`, `/clear done`, `/clear older than 30d`.
  Should confirm before deleting.

- **Auto-archive** — Background job that moves old done/query tasks to an `archived` status after N days (configurable). Keeps the active task list short without permanent deletion.

- **Dashboard bulk actions** — Bulk-select and delete (or archive) tasks from the Next.js dashboard UI.

- **Data API endpoints** — `DELETE /tasks/{id}` and `DELETE /tasks/bulk` (bulk by type/status/age filter).
  Neither endpoint is implemented yet. Both would require auth and confirmation semantics.
