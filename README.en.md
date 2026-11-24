# deliveryman

> Turns a project into a sanitized copy that is safe to share — a skill for Claude Code and other AI coding assistants.

📖 中文: [README.md](./README.md)

```bash
npx skills add SEAMAN9000/deliveryman
```

Before a project goes to someone else, its files often still hold things that should stay private: secret keys, the user name inside local paths, internal network addresses, development notes, the author name in a Word document's properties. deliveryman first scans the project read-only and shows an overview table. Only after you confirm does it create a clean copy inside the project. The source project is not modified, and the copy is not uploaded.

It is not limited to code projects: finished work such as reports, papers, slide decks and documents is handled the same way.

## When to Use

When a finished project has to go to someone else, checking every file for leftover keys, user names or internal notes takes time and is easy to get wrong. Sending the whole folder as it is means the recipient also gets your development notes and local paths. deliveryman turns that check into a table you can verify, then prepares a copy arranged for the person receiving it.

✅ A good fit:

- You are handing a code project to a colleague or collaborator who will read the code or keep working on it.
- You are sending a program, report, paper or slide deck to someone who will only use or read it.
- You are about to make a project public and first want to see what in it should not leave your machine.

❌ Not a fit:

- You want to upload the project to a code hosting site or create a repository: deliveryman only creates a local copy. Use git or the hosting site's own tools to upload.
- You want to remove author or camera information from PDFs and images, or sensitive text in the body of Office documents: it does not inspect these. Fix them in the source files.
- A key has already been committed to version history: the copy carries no version history, but the record in the original repository remains. Replace the key and clean the history with git's history-rewriting tools.

## What It Looks Like

Below is the record of one run on a sample project of just 7 files, shortened. Lines after "User:" are what the person said; lines after "AI:" are the assistant's replies. The original conversation was in Chinese; this is a translation.

```text
User: I want to send this project to a colleague to look at. Help me make a clean copy to share.

AI: What will your colleague do with this clean version? That decides which files go into the copy.
    - The colleague will read the code or keep working on it: includes the source code and all documentation (README, the documents in docs), removes key files, personal information and local paths.
    - The colleague will only use or read it: includes only the documentation and finished files, no source code by default, and likewise removes keys, personal information and local paths.
    - One of each: creates both versions above in two folders, and you choose which to send.

User: The colleague will read the code or keep working on it

AI: The project has been scanned and the copy has not been created yet. Please look at the list below and confirm first.

    7 files in total: 5 copied (1 with sensitive content replaced, 1 containing a local path awaiting your decision), 2 left out (1 of which you can ask to keep). The source project is read-only; no original file has been changed.

    | Project structure | Action | Notes |
    |---|---|---|
    | docs/ |  |  |
    | 　　guide.md | Copied |  |
    | 　　setup.md | **Copied, awaiting your decision** | Contains a local path (not replaced automatically): Windows local path ×1 |
    | src/ |  |  |
    | 　　config.py | **Copied with replacements** | Will replace: Windows user directory path ×1 |
    | 　　main.py | Copied |  |
    | .env | Left out | Key or password file |
    | CLAUDE.md | Left out | AI collaboration file, can be kept on request |
    | README.md | Copied |  |

    Files for you to look at (2):
    - docs/setup.md: contains a local path (not replaced automatically): Windows local path ×1
    - src/config.py: will replace: Windows user directory path ×1

    Settings for this run: packaged "for developers" (with source code and all documentation), AI collaboration files not kept. This project has not been packaged before, so no earlier choice is reused.

    Two items need your decision:

    1. docs/setup.md contains a path on your computer. The tool will not replace it automatically; after you confirm, it will appear in the copy unchanged.
    2. CLAUDE.md (project notes for the AI assistant) is left out by default. If your colleague also uses an AI assistant to keep working on the code, I can keep it.

    Replying y means you accept the path in item 1 going into the copy unchanged. You can also reply "adjust" (for example "keep CLAUDE.md as well") or "cancel".

User: y, go ahead

AI: The clean copy is ready, in the folder output/deliver/dev-2026-11-25-0330 inside the project. Send that whole folder to your colleague.

    - `.env`, which holds the key, and `CLAUDE.md`, the notes for the AI assistant, did not go into the copy.
    - 1 piece of sensitive content was replaced, so the user name no longer appears.

    There is only one known limit: your colleague will see this folder path.

    - Your original project has only one change: `.gitignore` was newly created this time and contains just the line `output/deliver/`, so the copy is not treated as part of the project itself. No other original file was touched.
    - The choices made this time have been recorded and will be reused by default next time. To start over completely, delete `output/deliver/_state.json`.

    I have not sent or uploaded anything for you.
```

What stays the same every time is the order of these steps and the three parts of the overview (one sentence of totals, the structure table, the files for you to look at); the files, numbers, items awaiting a decision and the assistant's wording change with the project. In the actual output, the folder and file names in the table are clickable links.

What the user does: say one sentence, answer who the copy is for, read the list, reply once to confirm.

## What It Does

1. **Scans and shows an overview table.** The table follows the project's folder structure and says what happens to each file, so a file with a problem stands out at once. Folder and file names are clickable:

   10 files in total: 6 copied (1 with sensitive content replaced, 1 containing a local path awaiting your decision), 4 left out (3 of which you can ask to keep).

   | Project structure | Action | Notes |
   |---|---|---|
   | .claude/agents/ (2 files) | Left out | AI collaboration files, can be kept on request |
   | docs/ | | |
   | 　　guide.md | Copied | |
   | 　　setup.md | **Copied, awaiting your decision** | Contains a local path (not replaced automatically): Windows local path ×1 |
   | node_modules/ | Left out | Whole folder skipped, files not scanned one by one |
   | src/ | | |
   | 　　core/ | | |
   | 　　　　a.py | Copied | |
   | 　　　　config.py | **Copied with replacements** | Will replace: Windows user directory path ×1 |
   | 　　main.py | Copied | |
   | .env | Left out | Key or password file |
   | CLAUDE.md | Left out | AI collaboration file, can be kept on request |
   | README.md | Copied | |

   Files for you to look at (2): docs/setup.md, src/core/config.py

   Folders whose files all go the same way and have no problems are folded into one row; a file with a problem is always listed on its own row.

   The table above is a translation; the skill's own output is in Chinese.

2. **Waits for your confirmation.** Every item that needs your decision is listed one by one. No file is created without your explicit consent.
3. **Creates the copy.** It goes into `output/deliver/<audience>-<timestamp>/` inside the project. That folder is the finished product: commit it to version control or copy the whole folder away.

## Two Audiences

You must say who the copy is for; the assistant will not guess.

| | For developers `dev` | For users `user` |
|---|---|---|
| Suited to | The recipient will read the code or keep working on it | The recipient will only use or read it |
| Source code, tests, build scripts | Included | Not included by default |
| Documentation | Root-level documents and the whole `docs/` folder | Only the user-facing parts: readme, license, getting-started guide |
| Extra steps | None | A runnable program gets double-click launcher files; for reports, papers and slide decks only the finished files you name are included |

Both audiences use exactly the same sanitizing rules.

**When the project being packaged is itself a skill**: the `user` audience recognizes the skill's folder automatically (by the `SKILL.md` inside it), includes everything in it except tests, plus the readme and license files from the project root, and makes that folder the root of the copy — the recipient can put it into their own skill directory and use it. The result of this recognition is shown in the report for you to confirm first.

## What It Handles

- **Whole files left out**: key and password files (`.env`, `*.pem` and so on), version control folders, dependency and cache folders, editor and system files, logs and temporary files, AI collaboration files (can be kept on request).
- **Content replaced inside files**: keys from mainstream providers, the user name in user directory paths, email addresses, internal network addresses.
- **Reported only, for you to decide**: local paths under custom drive letters or folders. They may be deliberate examples, so they are not replaced automatically.
- **Office documents**: author, company and template information is removed from the document properties of docx, pptx and xlsx files; internal traces such as comments and tracked changes are only counted and reported.

For what it cannot inspect (for example PDFs, images, the body of Office documents), see [`references/known-limits.md`](references/known-limits.md).

## Runtime Support

All three tools use the same instructions and scripts. deliveryman only needs the assistant to read files and run Python commands; it uses no feature that exists in just one tool.

### ✅ Claude Code

- Install location: `~/.claude/skills/deliveryman/` (available in every project), or `<your project>/.claude/skills/deliveryman/` (this project only).
- Project instruction file: `CLAUDE.md`.
- Trigger: say any phrase from the Trigger section, or type `/deliveryman dev`, `/deliveryman user` or `/deliveryman all`.

### ✅ Codex CLI

- Install location: `~/.agents/skills/deliveryman/`, or `<your project>/.agents/skills/deliveryman/`.
- Project instruction file: `AGENTS.md`.
- Trigger: say any phrase from the Trigger section.

### ✅ OpenCode

- Install location: `~/.config/opencode/skills/deliveryman/`, or `<your project>/.agents/skills/deliveryman/`.
- Project instruction file: `AGENTS.md`.
- Trigger: say any phrase from the Trigger section.

Each tool's project instruction file and configuration folder (`CLAUDE.md`, `AGENTS.md`, `.claude/`, `.codex/` and so on) count as AI collaboration files when packaging. They are left out of the copy by default and can be kept on request.

## Install

Requires Python 3.9 or later. The scripts use only Python's standard library; nothing else needs to be installed.

### One-click install

```bash
npx skills add SEAMAN9000/deliveryman
```

Requires Node.js. The installer detects which AI coding tools are on your machine and installs the skill into each one's location. By default it installs into the current project; add `-g` to install into your user directory so every project can use it; add `-a` to install for specific tools only.

Without the installer, follow the subsections below to install by hand: use git to download the repository straight into the skill directory of the tool you use. This requires git. The commands install into your user directory, so every project can use the skill; to use it in one project only, replace the destination with the in-project location from the previous section.

### Claude Code

```bash
git clone https://github.com/SEAMAN9000/deliveryman ~/.claude/skills/deliveryman
```

To confirm it is installed: open a new conversation in any project folder and type `/deliveryman dev`. If the assistant shows an overview table and waits for your confirmation, it is installed.

### Codex CLI

```bash
git clone https://github.com/SEAMAN9000/deliveryman ~/.agents/skills/deliveryman
```

The file name must stay `SKILL.md`; do not rename it.

To confirm it is installed: open a new conversation in any project folder and say "make a clean copy to share". If the assistant first asks whether the copy is for developers or for users, it is installed.

### OpenCode

```bash
git clone https://github.com/SEAMAN9000/deliveryman ~/.config/opencode/skills/deliveryman
```

To confirm it is installed: same as for Codex CLI.

## Trigger

| 中文 | English |
|---|---|
| `/deliveryman` | `/deliveryman` |
| 弄一个干净的版本 | make a clean copy to share |
| 发给同事前清理一下 | sanitize this project before sharing |
| 脱敏副本 | |

Phrases that do not trigger it: asking the assistant only to check or edit a particular file (for example "check the README for typos"), or asking only to upload or push.

To force it: in Claude Code, type `/deliveryman dev` or `/deliveryman user`; in Codex CLI and OpenCode, name it directly, for example "use deliveryman to make a copy of this project for developers".

## Usage

In the project you want to share:

```
/deliveryman dev
/deliveryman user
/deliveryman all
```

`all` makes one copy for each audience. You can also say it in your own words, for example "make a clean version of this project to send to a colleague"; the assistant will first ask who it is for.

Common adjustments can all be made in plain words:

- "Keep CLAUDE.md as well"
- "Remove notes/ as well"
- "Review everything again" — ignore decisions accepted before and go through every item again

The decisions you confirm are recorded in `output/deliver/_state.json`, so the next run highlights only newly found problems. Deleting this file resets everything.

## Project Configuration (Optional)

It works without any configuration. Settings that should apply every time go into `deliver.config.yaml` in the project root:

```yaml
custom_redactions:             # replacement rules specific to this project
  - name: internal_id
    pattern: '\bMYCO-\d{6}\b'
    replacement: '<REDACTED>'

keep:                          # always keep
  - docs/architecture.md

remove:                        # always remove
  - notes/personal.md

user:
  entry: dist/app.exe          # declaring an entry = this is a runnable program
  # artifacts: ["reports/**/*.pdf"]   # or name the finished files to deliver
```

For all fields, see [`templates/deliver.config.example.yaml`](templates/deliver.config.example.yaml).

## After a Run

**Steps**

1. The assistant scans the project read-only and shows the overview table. The scan runs on your machine; for a project of a few dozen files it takes less than a second.
2. The assistant lists every item that needs your decision and waits for your reply. This is the only step that needs your confirmation, and most of the time goes into reading the report.
3. After you agree, the assistant creates the copy. For a runnable program in the `user` audience there is one more step that creates the double-click launcher files; if the project configuration contains a build command, the assistant shows you the exact command first and runs it only after you agree.
4. The assistant reports where the copy is, how many files it holds, how many replacements were made, and which items you accepted into the copy unchanged.

**What is created**

```
your-project/
├── .gitignore                         # on the first run, one line is appended: output/deliver/
└── output/deliver/
    ├── dev-2026-01-15-1030/           # the copy; the folder name is <audience>-<timestamp>
    ├── _reports/                      # scan reports, run records, your words of confirmation
    ├── _state.json                    # decisions you confirmed, reused next time
    └── deliver.config.suggested.yaml  # created only when you kept or removed extra files for this run
```

The copy of a runnable program also contains two launcher files, `RUN.bat` and `run.sh`. The result is the same on all three tools.

**What changes**

Changed: files under `output/deliver/`, and that one line at the end of `.gitignore`. Not changed: any other file in the project, including `deliver.config.yaml`.

**If something fails**

- You have not confirmed yet, or you scanned again after confirming: the script that creates the copy refuses to run and produces no files.
- Individual files fail while the copy is being created: the failed files are listed, and the full error messages are under `output/deliver/_reports/`. The timestamped copy folder may then be incomplete; do not share it.
- Starting over: the source project has not been modified, so simply run it again. Every run creates a new timestamped folder, and the incomplete one can be deleted.

## Three Promises

- **The source project is read-only.** Everything is written under `output/deliver/`, and the scripts block any write outside it. The only exception is appending the line `output/deliver/` to the end of `.gitignore` on the first run.
- **Report first, create only after confirmation.** The script that creates the copy runs only when it is given your exact words of confirmation.
- **No upload.** It only creates a local copy; it does not push and does not create a repository.

## Directory Structure

```
deliveryman/
├── SKILL.md          # instructions for the assistant
├── references/       # additional notes, read when needed
├── rules/            # sanitizing rules: which files to leave out, what content to replace, which files each audience gets
├── scripts/          # scripts that scan, create the copy and create launcher files
└── templates/        # configuration example and launcher file templates
```

## License

MIT — see [LICENSE](./LICENSE)
