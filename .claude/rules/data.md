# Data / Schema Rules

Binding for any code touching the database. Carried over from Amber's own VideoAmp/R-EX lessons-learned — proven principles, not new ones being tried out here.

- **Nullable over sentinel values.** When a field isn't known until a later step (embedding not generated yet, extraction pending), make the column nullable. Don't invent magic placeholders like `0.01` or `1970-01-01` — every one becomes a landmine in a later query that forgot it was magic.
- **Explicit column lists, never `SELECT *`.** A `SELECT *` hides schema drift (new column silently breaks a row-scan) and hides which columns can be NULL at the call site. Keep a named column-list constant per table.
- **Marshal JSON from typed structs/models, never hand-format it.** String-concatenating JSON is an escaping bug waiting to happen. Let the serializer (Pydantic, json.dumps on a typed structure) handle quoting and nulls.
- **Schema comments describe observable state, not project lore.** A column comment says what the data *is*. It doesn't reference this job application, a phase name, or today's design rationale — that outlives its usefulness and becomes stale noise.
- **Best-effort cleanup on partial failure.** If one operation has multiple side effects (write a blob and a row; call an embedding API and insert a vector), and a later step fails, clean up the earlier successful step. Best-effort, logged, and return the *original* error — not the cleanup error.
- **Fail loud, never silently empty.** If a parser or extractor stops finding what it expects (an uploaded PDF yields no text, a spreadsheet import finds no recognizable header), raise or log an explicit error with a stored reason. Never let a broken step quietly return zero results that look like "nothing there."
- **Source attribution on every item.** Every ingested item carries, at minimum, its source (URL, upload URI, or document reference), its own effective/version date where known, and the timestamp this system ingested it. Factual claims in generated answers must trace back to these.
