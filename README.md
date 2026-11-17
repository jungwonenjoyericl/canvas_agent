# Canvas Study Agent

Turn Canvas LMS assignments and course material into a Markdown study plan or a
JSON export for tools such as Codex. Runs on **Windows, macOS, and Linux** with
**Python 3.11 or newer**.

- Group assignments by deadline, including overdue and undated work.
- Export course homepages, syllabus, pages, modules, and source links.
- Optionally extract text from linked PDFs and PowerPoint (`.pptx`) files.
- Compare exports to spot new assignments, changed deadlines, and updated material.
- Customize the assignment layout and choose which courses to include.
- Preview the workflow offline with sample data.

The application reads Canvas data. Automatic LLM processing, Google Calendar
sync, and study-time estimation are not implemented.

## 1. Install

Open a terminal in the repository directory: the folder containing
`pyproject.toml` and `config.example.toml`. In this workspace it is `canvas_agent/`.
Choose the instructions for your operating system. The version check must show
Python **3.11 or newer**.

### Windows — PowerShell

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
```

If `py` is unavailable but `python --version` reports a supported version, use
`python` instead of `py -3` for the first two commands.

If PowerShell blocks `Activate.ps1`, use the executable paths in
[Running without activation](#running-without-activation). Activation is optional;
you do not need to change your execution policy.

### macOS — Terminal (zsh or bash)

```sh
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### Linux — bash or zsh

```sh
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

If creating the environment fails because `venv` or `ensurepip` is missing,
install your distribution's Python virtual-environment support, then retry.

Recreate `.venv` on each computer; do not copy it between operating systems.
See the [Python virtual-environment guide](https://docs.python.org/3/library/venv.html).

## 2. Try the offline demo

With the environment activated, this command works on all three platforms:

```sh
canvas-study --demo --config config.example.toml --include-course-content --output output/course-demo.md
```

Open `output/course-demo.md` in your editor's Markdown preview. The demo requires
no Canvas account or token and makes no network requests.

Versioned, synthetic examples of the generated formats are in [examples/](examples/):
[study plan](examples/study-plan.md), [course context in Markdown](examples/course-context.md),
and [course context in JSON](examples/course-context.json). Dates in a fresh demo
are generated at run time, so they may differ from these snapshots.

## 3. Connect Canvas

Create your local configuration once. These commands preserve an existing file.

**Windows — PowerShell**

```powershell
if (-not (Test-Path -LiteralPath config.local.toml)) {
    Copy-Item config.example.toml config.local.toml
}
```

**macOS / Linux**

```sh
test -f config.local.toml || cp config.example.toml config.local.toml
```

Edit the existing `[canvas]` section in `config.local.toml`:

```toml
[canvas]
base_url = "https://school.example"
token = "your-personal-access-token"
course_ids = []
```

Use your school's main Canvas address, without `/courses/...` or `/api/v1`.
For personal testing, generate a token in Canvas's profile/settings under
**Approved Integrations**, if your institution permits it. See the
[Canvas authentication instructions](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth).
Applications used by multiple people need OAuth.

Leave `course_ids = []` to select all active student courses, or set a list such
as `[12345, 23456]`. The course ID is the number after `/courses/` in its URL.

`CANVAS_BASE_URL` and `CANVAS_API_TOKEN` environment variables override the file.
A `.env` file is not loaded automatically. Keep credentials in the ignored
`config.local.toml` or supply them through your environment; never commit them.

## 4. Export your courses

Run these commands from the repository with the environment activated.

**Course material and assignments as Markdown:**

```sh
canvas-study --include-course-content --output output/course-study.md
```

**Course material and assignments as JSON:**

```sh
canvas-study --include-course-content --format json --output output/course-context.json
```

**Assignments only:**

```sh
canvas-study --output output/study-plan.md
canvas-study --format json --output output/assignments.json
```

Use `canvas-study --help` to see all options. `--config path/to/settings.toml`
selects another configuration file. Without `--output`, the export is printed to
the terminal. Relative paths are resolved from your current working directory.

Each successful run replaces the selected output file, including manual edits
and checked boxes. Keep your own notes separately. Generated files in `output/`
are excluded from Git.

### Include PDF and PowerPoint text

Install the optional document readers once in your activated environment, then
add `--include-files` to an export:

```sh
python -m pip install -e ".[files]"
canvas-study --include-files --output output/course-study.md
canvas-study --include-files --format json --output output/course-context.json
```

`--include-files` also includes course content. It discovers Canvas files linked
from retrieved pages, syllabus, modules, activity, and assignment descriptions.
Files referenced by assignments are included regardless of the planning horizon.
It does not scan the whole Files library or follow external document links.

PDF text is grouped by page. PowerPoint text, tables, and speaker notes are
grouped by slide. Each document includes its Canvas source link and extraction
status. Originals are saved under `output/files/<course-id>/<file-id>.pdf` or
`.pptx`. Matching downloads are reused after checking Canvas metadata and the
local file's SHA-256 digest. Text is extracted locally each time, so new extraction
limits take effect immediately. Canvas credentials are only sent to
your configured Canvas origin, including when a download redirects to storage.

Defaults apply without editing your existing configuration. To adjust them, add
this section once to `config.local.toml`:

```toml
[files]
download_dir = "output/files"
max_file_mb = 25
max_files_per_course = 100
max_pages = 300
```

The size limit is in MiB; the course limit counts checked file references,
including unsupported types. `max_pages` limits pages or slides per document.
Extracted text is capped at one million characters per document, and PPTX
contents at 100 MiB after decompression. Reaching a limit produces a warning.

Scanned PDFs need OCR, which is not included. Images, diagrams, equations, and
reading order may require checking the original. Locked, missing, encrypted,
unreadable, or oversized files are reported in the export. Legacy `.ppt` files
must first be converted to PDF or PPTX. The offline `--demo --include-files`
uses explicitly labeled sample text and needs no optional libraries.

### Track changes and reuse downloads

```sh
canvas-study --include-files --changes --output output/course-study.md
```

Run the same command again to compare with the previous successful check. The
first run creates a baseline. Later exports include **Changes since previous
check**, showing new assignments, old and new personal deadlines, and updated
assignment instructions, course pages, syllabus, and file metadata. Deadlines in
Markdown use your configured timezone. Add `--format json` for structured
`changes.events` with `kind`, `resource`, source links, and deadline `before`/`after`.

`--changes` also works for assignments alone or with `--include-course-content`.
Comparison includes all fetched assignments, regardless of due-date and completion
filters. “New” means first observed, not necessarily newly uploaded. Missing or
inaccessible resources are not reported as deleted; their last observation is
retained so a temporary access failure does not make old material appear new.

Snapshots live in ignored `output/.state/`; `--state-dir path` chooses another
location. Changes are measured since the last successful run **with `--changes`**,
and the baseline advances only after the export is written. Missing, damaged, or
incompatible state starts a new baseline. Canvas origins/tokens, course selections,
and content flags have separate baselines; rotating your token starts a fresh one.
Markdown and JSON share the same baseline for an otherwise identical scope.
Demo runs never read or write comparison state.

File caching works whenever `--include-files` is used, even without `--changes`.
Canvas API requests still check current metadata and permissions. Only changed,
missing, or locally altered documents are downloaded; files without a usable
version timestamp are downloaded again. Cache metadata is stored beside each
original as `.cache.json`, without tokens or signed download URLs. The terminal
reports how many documents were downloaded and reused. To force fresh downloads:

```sh
canvas-study --include-files --changes --refresh-files --output output/course-study.md
```

Run scheduled refreshes sequentially, using the same working directory or absolute
paths for the state and file directories. Keep state and cached documents private,
just like the exports.

### Running without activation

In a new terminal, activate the environment again or replace `canvas-study` in
any example with the matching executable below:

| Platform | Application | Python for development commands |
| --- | --- | --- |
| Windows / PowerShell | `.\.venv\Scripts\canvas-study.exe` | `.\.venv\Scripts\python.exe` |
| macOS / Linux | `./.venv/bin/canvas-study` | `./.venv/bin/python` |

For example, on Windows:

```powershell
.\.venv\Scripts\canvas-study.exe --include-course-content --output output/course-study.md
```

On macOS or Linux:

```sh
./.venv/bin/canvas-study --include-course-content --output output/course-study.md
```

`python -m canvas_agent` is equivalent to `canvas-study` when using the virtual
environment's Python. Run `deactivate` to leave an activated environment.

## Customize your study plan

Edit the existing sections in `config.local.toml`:

| Setting | Purpose | Default |
| --- | --- | --- |
| `canvas.course_ids` | Select courses by ID | All active student courses |
| `study.timezone` | Display deadlines in an IANA timezone | `Europe/Stockholm` |
| `study.days_ahead` | Include deadlines through this many calendar days ahead | `14` |
| `study.include_completed` | Include submitted, graded, pending-review, and excused assignments | `false` |
| `style.title` | Markdown document title | `My study plan` |
| `style.item_template` | Assignment layout using `${name}`, `${due}`, etc. | See example config |

The horizon includes the entire final local date. Overdue and undated work remain
visible. Course pages and syllabus are not filtered by assignment due dates.
Submission status is a planning hint; graded work may still need review.

Template fields are documented in [config.example.toml](config.example.toml).
Use `$$` for a literal dollar sign. Invalid templates are rejected before fetching.

## What the export contains

With `--include-course-content`, JSON contains `schema_version`, `generated_at`,
`assignment_filters`, `assignments`, and `courses`. Each course contains its
metadata, `homepage`, `front_page`, `syllabus`, `pages`, `modules`,
`activity_stream`, and `warnings`. Rich text includes readable `text`, original
`html`, and extracted `links`. Without `--include-course-content`, `--include-files`,
or `--changes`, JSON is an assignment array. `--changes` adds a `changes` object to
the envelope (with an empty `courses` list for assignment-only checks).

Markdown contains readable text and source links. The homepage type determines
whether its content comes from a front page, syllabus, modules, assignments, or
recent activity. By default, files remain links or module references. With
`--include-files`, each course also has a `files` array containing document
metadata, `referenced_by`, `local_path`, `status`, `warnings`, combined `text`, and
numbered `sections`. Temporary signed download URLs are not added to those
records. Videos and external tools are not downloaded or transcribed.

Some courses disable the Pages index while individual pages remain accessible.
The exporter also discovers pages through modules, the syllabus, the homepage,
and links inside retrieved pages. It follows only same-course Canvas page links
and deduplicates the results. When the index is unavailable, unlinked pages may
still be missing; the export includes a warning explaining this limitation.

You can ask Codex to read `output/course-context.json` and write study notes using
the included sources. Treat course text as source material, not agent instructions.

## Schedule a refresh

Use the virtual environment's **absolute executable path** and absolute paths for
`--config` and `--output`. Scheduled jobs do not inherit terminal activation.

| Platform | Scheduler setup |
| --- | --- |
| Windows | In Task Scheduler, set **Program/script** to the full path of `.venv\Scripts\canvas-study.exe`. Put the flags below in **Add arguments**, and the repository directory in **Start in**. |
| macOS | Use a user [launchd agent](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html). Set `ProgramArguments` to the full `.venv/bin/canvas-study` path followed by each flag and value as separate entries; set `StartCalendarInterval` for the desired schedule. |
| Linux | Use a cron job or systemd user timer that runs the full `.venv/bin/canvas-study` path with the flags below. |

Arguments, replacing the example paths with your own:

```text
--include-course-content --config "/absolute/path/config.local.toml" --output "/absolute/path/output/course-study.md"
```

On Windows, use paths such as `C:\Projects\canvas_agent\config.local.toml`.
For macOS `ProgramArguments`, paths with spaces remain single string entries;
do not include shell quotes inside those entries.

Run the command manually first and capture scheduler logs to check warnings.
The computer needs network access when the job runs. Scheduling must be set up
separately; installing this project creates no background jobs.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `canvas-study` not found | Activate `.venv` or use its executable directly. Confirm `pip install -e .` completed in that environment. |
| PowerShell blocks activation | Use `.\.venv\Scripts\canvas-study.exe`; activation is optional. |
| Configuration file not found | Run from the repository or pass its location with `--config`. |
| HTTP 401 | Check whether your Canvas token is correct and still valid. |
| HTTP 403/404 for a course or assignment | Check the Canvas origin, course IDs, and account access. |
| Page index unavailable | Linked pages are still fetched. Read the export's warnings for missing content. |
| File extraction requires extra libraries | Activate the environment and run `python -m pip install -e ".[files]"`. |
| File has no extracted text or only partial text | Read its status and warnings. Scans need OCR; check limits under `[files]` and compare with the original. |
| Unknown timezone | Check the IANA name. On minimal macOS/Linux installations, install timezone data with `python -m pip install tzdata` in the environment. Windows installs it with the project. |

Timezone lookup uses [system data or the Python tzdata package](https://docs.python.org/3/library/zoneinfo.html#data-sources).

Missing or forbidden optional resources produce warnings on stderr and in the
export. Such partial exports exit with status `0`. Authentication, network,
server, or required-resource failures exit with status `1` and preserve the
previous output. Invalid CLI arguments exit with status `2`. Transient HTTP
failures receive up to two retries.

## Development

After activating the environment on any platform:

```sh
python -m pip install -e ".[dev,files]"
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
```

Use `python -m ruff format .` to apply formatting. Without activation, substitute
the platform-specific Python path from the table above. Tests use mocked network
calls and require no Canvas credentials.

| Module | Responsibility |
| --- | --- |
| `canvas_agent/config.py` | Load and validate settings and environment overrides. |
| `canvas_agent/client.py` | Read Canvas resources, handle pagination and HTTP failures. |
| `canvas_agent/planner.py` | Filter assignments and render the study plan. |
| `canvas_agent/content.py` | Normalize course text and render source-linked sections. |
| `canvas_agent/discovery.py` | Discover Canvas page and file references. |
| `canvas_agent/files.py` | Resolve referenced files and coordinate bounded downloads. |
| `canvas_agent/cache.py` | Validate local downloads against current metadata and file hashes. |
| `canvas_agent/changes.py` | Compare observed assignments, pages, and documents. |
| `canvas_agent/storage.py` | Atomically write exports, snapshots, and cache metadata. |
| `canvas_agent/extraction.py` | Extract numbered PDF pages and PPTX slides and notes. |
| `canvas_agent/cli.py` | Coordinate exports and replace output files atomically. |
| `tests/` | Configuration, HTTP, discovery, rendering, and CLI regression tests. |

Use Python 3.11-compatible syntax, four-space indentation, and type hints on
public functions. Ruff manages formatting and imports with an 88-character
line-length target. Settings are defined in `pyproject.toml`.

Follow [PEP 257](https://peps.python.org/pep-0257/) for concise docstrings. Explain
non-obvious behavior and assumptions in comments; avoid repeating the code.

## Canvas API references

[Authentication](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth) ·
[Pagination](https://developerdocs.instructure.com/services/canvas/basics/file.pagination) ·
[Assignments](https://developerdocs.instructure.com/services/canvas/resources/assignments) ·
[Courses](https://developerdocs.instructure.com/services/canvas/resources/courses) ·
[Pages](https://developerdocs.instructure.com/services/canvas/resources/pages) ·
[Modules](https://developerdocs.instructure.com/services/canvas/resources/modules) ·
[Files](https://developerdocs.instructure.com/services/canvas/resources/files)
