# Roadmap

What has been asked for and is not built yet. One entry per idea, newest first, carrying
what is already known about it. This is not a schedule: it is a notebook, so that nothing
is lost between two sessions.

An idea that gets built leaves this file and goes into `CLAUDE.md`, where the facts live.

---

## Pick a destination folder while uploading

**Asked for on 2026-08-26.** On the import page, say which folder the rushes should land
in, choosing an existing one or creating one on the way. Without it everything arrives in
Global and has to be filed by hand, rush by rush, afterwards.

**What makes this less trivial than it looks**: uploading and ingesting are decoupled. An
upload drops files into `inbox/` and stops there; the **scan** is what creates the clips
and sequences, later, and it knows neither who dropped what nor where they wanted it to
go. So the intent has to survive from one to the other.

Two ways to carry it, and the cheaper one might be enough:

- **Client side, after the fact.** The scan's response already names the sequences it
  created, so the page could set the folder on each of them in one request apiece.
  Nothing to store server side. It breaks in two cases: when the **scheduled** scan is
  the one that ingests (so when the tab is closed, or when the upload outlasts the next
  tick), and when two people are dropping files at once.
- **Server side, carried by the upload.** `POST /upload/begin` would take a `folder_id`,
  held against the resolved file name, and `ingest_and_group` would read it when creating
  the sequence. Sturdier, and it needs somewhere to keep that intent: a column on a table
  of uploads in flight, or a file next to the `.partial`.

**Two things not to forget**: folders go **two levels deep at most** (a site, and an
outing inside it), the rule lives in the API, and creating one from the import page has
to respect it like everywhere else. And Global is not a row in the database, it is
`folder_id = null`, so "no folder" stays a valid choice and has to remain the default.
