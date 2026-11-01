# Security policy

## Supported versions

Security fixes are made on the latest release and on `main`.

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue. Use GitHub's
private vulnerability reporting: open the repository's **Security** tab and
choose **Report a vulnerability**.

Include the CIRCA version (`circa --version`), the command you ran, and a
minimal reproduction. Use synthetic values only: never include real
credentials, private CI logs, or customer data in a report, even a private one.

## What CIRCA does to protect CI data

CIRCA runs locally. It never sends logs to a cloud service; the optional LLM is
a local Ollama server, and GitHub access goes through your own `gh` login, whose
token CIRCA never reads.

### Credential redaction

Recognized credential values are replaced with `[REDACTED]` before any
processing. Extraction, analysis, fingerprints, historical matching, LLM
prompts, drafts, and terminal and JSON output all use the redacted text. See
[Secrets redaction](README.md#secrets-redaction) for the patterns that are
recognized and the ones that are deliberately left alone.

Redaction is best-effort, not complete secret detection. It does not find:

- high-entropy strings with no recognizable name or prefix;
- values shorter than four characters;
- secrets split across lines;
- credential formats that are not listed.

Review logs before sharing them, keep Gitleaks (or a similar scanner) in CI, and
rotate any credential that appears in a CI log, whether or not CIRCA redacted it.

### Fail-closed behavior

If redaction raises any error, CIRCA stops:

- the command prints `Error: credential redaction failed, so CIRCA stopped without using the unredacted text`
  and exits with status 1;
- nothing reads the original log: no fingerprint, report, LLM call, or draft is
  produced;
- the error carries no log text and does not chain the underlying exception.

There is no fallback to unredacted content. LLM output gets the same treatment.
A credential in it is redacted, and the draft is not created, because the model
was never shown one.

## Knowledge files

A team knowledge file (for example `team/incidents.json`) is reviewed data. Keep
it in version control so every change goes through a pull request.

- Records hold short single-line evidence excerpts, never raw logs.
- A record whose text contains a recognized credential is rejected when it is
  added, updated, or loaded. CIRCA never redacts a record silently.
- Errors name the record and field, such as
  `record 2: ... in evidence[1]; replace each credential with [REDACTED]`. They
  never print the value.
- `circa analyze` and `circa analyze-log` never write the file;
  `investigate-log` only adds `pending_review` drafts.

### Repairing a knowledge file

A file written by CIRCA 0.1.0 can fail to load after upgrading, because
credential detection is broader. CIRCA does not change or delete records
itself. To repair one:

1. Run `circa knowledge validate path/to/incidents.json` to see the record
   number and field.
2. Rotate the exposed credential: it was stored in the file and may be in your
   Git history.
3. Edit the file and replace only the credential value with `[REDACTED]`. Keep
   the rest of the line.
4. Run `circa knowledge validate` again until it reports `valid`, then commit
   the change for review.

If you change a record's `evidence`, its stored dedup fingerprint no longer
matches the new text. Exact-fingerprint matching then stops working for that
record, but keyword matching still does. To get a fresh fingerprint for a
`pending_review` record, run `circa knowledge show ID --json` and pass its
output to `circa knowledge update`. A `verified` record cannot be updated: add a
corrected incident, verify it, and run `circa knowledge supersede` on the old
one.

## Scope

Reports about the following are welcome:

- credential leaks in output, drafts, or prompts;
- bypasses of fail-closed redaction or knowledge validation;
- command or path injection;
- unsafe terminal output.

Missing a credential format that CIRCA does not claim to recognize is a feature
request, not a vulnerability.
