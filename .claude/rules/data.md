# Data / Schema Rules

Binding for any code touching the database. Carried over from Amber's own VideoAmp/R-EX lessons-learned — proven principles, not new ones being tried out here.

- **Nullable over sentinel values.** When a field isn't known until a later step (embedding not generated yet, extraction pending), make the column nullable. Don't invent magic placeholders like `0.01` or `1970-01-01` — every one becomes a landmine in a later query that forgot it was magic.
- **Explicit column lists, never `SELECT *`.** A `SELECT *` hides schema drift (new column silently breaks a row-scan) and hides which columns can be NULL at the call site. Keep a named column-list constant per table.
- **Marshal JSON from typed structs/models, never hand-format it.** String-concatenating JSON is an escaping bug waiting to happen. Let the serializer (Pydantic, json.dumps on a typed structure) handle quoting and nulls.
- **Schema comments describe observable state, not project lore.** A column comment says what the data *is*. It doesn't reference this job application, a phase name, or today's design rationale — that outlives its usefulness and becomes stale noise.
- **Best-effort cleanup on partial failure.** If one operation has multiple side effects (write a blob and a row; call an embedding API and insert a vector), and a later step fails, clean up the earlier successful step. Best-effort, logged, and return the *original* error — not the cleanup error.
