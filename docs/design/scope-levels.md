# Design: access levels per scope entry

Status: proposal, for review. Nothing here is built yet.

## Why

The broker checks two things separately: the scope (*where*: `gh:OWNER/REPO`, `gh-project:OWNER/N`,
`rad:RID`, `ssh:HOST`, `git-local:PATH`) and one role per pod (*which actions*, a flat list). They
don't line up. `apod fryorcraken --scope 'gh:logos-co/*' --scope 'gh-project:logos-co/*' --preset
logos` still asked before `gh project item-list 18 --owner logos-co` ("not allowed for role dev:
gh:project item-list"): the entry says "work on the boards", but `dev` has no project actions, and
`pm`, which has them, can't push or sign. Real jobs mix levels by target: write on one repo, read
on the rest of the org, write on a board. Today the only way to get that is a made-up role in
`roles.toml`.

## The model

Each scope entry carries a **level**: `gh:logos-co/flywheels.logos.co=write`. Each scope kind
declares its levels, in order, and the actions at each; a level includes the ones before it. An
action on a target is allowed outright when the target's level includes it. An entry written
without a level gets the session's **default level** for its kind: the profile's or preset's
`default_level`, else the kind's own default. `allow` and `deny` stay, for actions without a
target and for anything finer than a level, and `HARD_DENY` still wins over everything. The
pod-wide role goes: `role` and `--role` remain as names for a bundle of default levels (plus
`allow` for what no level holds), so every existing launch means what it meant.

Every kind also has the level `none`, below its first: the target is in scope (a request may refer
to it, and `allow` applies to it), but the level allows nothing.

## Syntax

```sh
apod work --scope 'gh:logos-co/*=read' --scope 'gh:logos-co/flywheels.logos.co=write' \
          --scope 'gh-project:logos-co/*=write' ~/src/logos-co/flywheels.logos.co
```

```toml
# profiles/work.toml, or presets/logos.toml
scope = ["gh:logos-co/*=read", "gh-project:logos-co/18=write", "ssh:nuc=login"]
default_level = { gh = "write", rad = "write" }   # entries without =LEVEL, the project's remotes too
allow = ["notify:send"]                            # actions on anything in scope, as today
deny = ["github:push force"]
```

- `KIND:VALUE=LEVEL`. The broker splits at the last `=` only when what follows is one of the
  kind's levels, so a value with `=` in it (a path) still works. An unknown level is a warning at
  launch and the entry is dropped, like an unknown kind.
- The same value twice, or two patterns that match one target: the **highest** level wins. Entries
  only grant, never take away, so presets and the profile add up in any order, as scope does
  today. To keep one repo read-only inside an org you write to, list the repos you write to
  instead of `OWNER/*`.
- `default_level` is a table of kind = level, in a profile and in presets. A preset's is over the
  profile's, kind by kind; two presets with different levels for one kind collide, like two
  images do. `--role NAME` replaces all of them for the launch.
- `maintain` (on `gh:` and `rad:`) can't be a default: `default_level = { gh = "maintain" }` is an
  error that names the entries to write instead. Merging, tags, ref deletions and Radicle
  default-branch pushes always name their repositories.
- Presets gain `allow` and `deny` (today they can't have them), added to the profile's. A preset's
  `role` needs them to translate exactly (maintainer, ops), and they are about *what*, not *who*.

## Levels

Taken from today's `roles.toml`. "No target" marks the actions that come without one (see
[question 4](#4-actions-without-a-target)). Actions marked *new* aren't in any role today.

### `gh:` (owner: gh; github and git-sign add to it; gh-api uses gh's classifier)

Default `read`. `gh:OWNER/REPO` is a repository, `gh:OWNER` the owner itself: one set of levels, and each
action applies to whichever form its target has (`repo create` targets `gh:OWNER`).

- **`read`**
  - gh: `pr list`, `pr view`, `pr diff`, `pr checks`, `pr status`, `issue list`, `issue view`,
    `issue status`, `run list`, `run view`, `run watch`, `workflow list`, `workflow view`,
    `repo view`, `label list`, `release list`, `release view`, `cache list`, `ruleset list`,
    `ruleset view`, `api GET /repos/{repo}`, `api GET /repos/{repo}/*`,
    `graphql query repository`, `graphql query node`, `graphql query nodes`.
  - gh, on `gh:OWNER`: `graphql query organization`, `graphql query user`,
    `graphql query repositoryOwner`.
  - gh, no target: `repo gitignore *`, `repo license *`, `api GET /rate_limit`,
    `graphql query rateLimit`.
  - github: `fetch`; no target: `fetch public`.
- **`triage`**
  - gh: `issue create`, `issue comment`, `issue edit`, `issue close`, `issue reopen`,
    `pr comment`, `label create`, `label edit`, `api POST /repos/{repo}/issues/*/comments`,
    `api PATCH /repos/{repo}/issues/comments/*`, `graphql mutation addComment`,
    `graphql mutation updateIssue`, `graphql mutation addLabelsToLabelable`,
    `graphql mutation removeLabelsFromLabelable`.
- **`write`**
  - gh: `pr create`, `pr edit`, `pr ready`, `api POST /repos/{repo}/pulls/*/comments`,
    `api POST /repos/{repo}/pulls/*/comments/*/replies`,
    `graphql mutation addPullRequestReviewComment`, `graphql mutation addPullRequestReviewThread`,
    `graphql mutation addPullRequestReviewThreadReply`, `graphql mutation resolveReviewThread`.
  - github: `push branch`, `push force`.
  - git-sign: `commit`, `tag` (for a repository whose push remote this is).
- **`maintain`**, only on entries that say `=maintain`
  - gh: `pr merge`, `pr review`, `pr close`, `pr reopen`, `pr update-branch`, `run rerun`,
    `run cancel`, `workflow run`, `graphql mutation mergePullRequest`,
    `graphql mutation addPullRequestReview`, `graphql mutation submitPullRequestReview`.
  - gh, on `gh:OWNER`: `repo create`; `repo fork` (on the owner it forks into).
  - github: `push tag`, `push delete`.

In no level, so behind approval or `allow`, as today: `github:push default-branch`,
`github:push other-ref`, `gh:pr merge --admin`, `gh:repo create --public`, and the gh commands no
role has (`repo edit`, `repo delete`, `secret`, `variable`, `release create`, `gist`, ...). No
`admin`: repository settings and deletions stay behind approval.

### `gh-project:` (owner: gh)

Default `read`.

- **`read`**: `project list`, `project view`, `project item-list`, `project field-list`,
  `graphql query node`, `graphql query nodes`.
- **`write`**: `project item-add`, `project item-edit`, `project item-archive`,
  `project item-create`, `issue edit --add-project`, `issue edit --remove-project`,
  `pr edit --add-project` (*new*), `pr edit --remove-project` (*new*); `graphql mutation`
  `addProjectV2ItemById`, `updateProjectV2ItemFieldValue`, `updateProjectV2ItemPosition`,
  `clearProjectV2ItemFieldValue`, `archiveProjectV2Item`, `addProjectV2DraftIssue`.
- **`admin`**: `project field-create`, `project mark-template`, `project copy`, `project link`;
  `graphql mutation` `createProjectV2Field`, `updateProjectV2Field`.

In no level: `project create`, `edit`, `close`, `delete`, `field-delete`, `item-delete`,
`unlink`, `unmark-template`.

### `gh-search:` (owner: gh)

One level, `read` (the default): `search issues`, `search prs`, `search code`, `search commits`,
`search repos`. Being in scope is what matters, as today.

### `rad:` (owner: rad; git-sign adds to it)

Default `read`.

| Level      | Adds                                                                                         |
| ---------- | -------------------------------------------------------------------------------------------- |
| `read`     | rad: `fetch`.                                                                                |
| `write`    | rad: `push branch`, `push patch`, `push force`. git-sign: `commit`, `tag`.                   |
| `maintain` | Only on entries that say `=maintain`. rad: `push default-branch`, `push tag`, `push delete`. |

In no level: `rad:push other-ref`.

### `ssh:` (owner: ssh)

Default `connect`. `connect`: `ssh:connect`. `login`: `ssh:login`. In no level: `ssh:connect port`
(a tunnel to another service), `ssh:sign other`.

### `git-local:` (owner: git-sign)

Default `sign`. `sign`: `git-sign:commit`, `git-sign:tag`. In no level: `git-sign:as other`,
`git-sign:other`.

### Without a kind

`clipboard:image`, `notify:send`, `notify:urgent`: `allow` only (no built-in role has them today
either).

## The questions

### 1. Binding actions to targets

**Rule:** an action is checked against every target of the request whose kind lists it, at any
level; each of those targets' level must include it. Targets of kinds that don't list it only have
to be in scope (any level, `none` too). An action that no target's kind lists is allowed only by
`allow`. Requests without any target follow [question 4](#4-actions-without-a-target).

So `gh project item-add 18 --owner logos-co --url https://github.com/logos-co/x/issues/5` needs
`write` on `gh-project:logos-co/18` and only scope on `gh:logos-co/x`: gh's table doesn't list
`project item-add`. `git-sign:commit` for a repository that pushes to GitHub is checked against
`gh:`, because github's and git-sign's additions put it in gh's `write`.

**Narrowing, where the classifier knows better:** `req.bind(action, targets)` checks that action
against those targets only (they're added to `req.targets` too, so they're still scope-checked).
Without a binding, the rule applies, and it errs strict: it can ask for too much, never allow too
much. Full (action, target) pairs everywhere would mean rewriting every classifier for little
gain: most requests have one action and one target.

What classifiers do differently:

- **gh** (`extensions/gh.py`):
  - GraphQL: `classify_graphql` already finds targets per top-level field; it binds each field's
    action to them. A query that reads repo B and mutates an issue in repo A then needs `triage`
    on A only.
  - `project copy` binds to its target owner (`gh-projects-of`), not the source board.
  - `repo fork` binds to `gh-fork-into` (where the fork goes), not the source.
  - `--add-project` and `--remove-project` (issue and pr edit) name projects by title, which today
    aren't targets at all. The classifier looks the titles up among the repository owner's and the
    account's projects (`gh project list --format json`, cached like node ids) and binds the flag's
    action to the `gh-project:` targets; a title it can't resolve is an `unscoped` target, so it
    asks.
  - Fix while there: a selector that's a URL (`gh issue comment https://github.com/a/b/issues/3`)
    acts on that repository, so the clone's repository isn't a target then. Today both are, which
    with levels would ask for `triage` on a repository the command doesn't touch.
- **gh-api, github, rad, ssh, clipboard, notify**: no change.
- **git-sign**: no change to the classifier. At setup, `configure` turns on `commit.gpgsign` when
  `allowed(sess, "git-sign:commit", T)` for the project's own target T (its push remote, else its
  folder), not "anywhere": otherwise a pod with `gh:REMOTE=read` and a `git-local:` folder would sign
  by default and ask on every commit.

### 2. Kinds that several extensions use

The kind's owner (the extension that calls `scope_kind`) declares the levels: their names, order,
default, which are explicit-only, a line of help each, and its own actions. Other extensions add
actions to a level by name:

```python
level_actions("gh", "write", ["github:push branch", "github:push force"])   # github.py
level_actions("rad", "write", ["git-sign:commit", "git-sign:tag"])          # git_sign.py
```

- They can add actions to existing levels only, never add or reorder levels.
- Additions are kept by the core and applied when the kind is declared, so load order doesn't
  matter (`git_sign` loads before `rad`). Additions to a kind no extension declares are dropped;
  to a level the kind lacks, dropped with a line in `broker.log`.
- `apod broker extensions NAME` shows them under both extensions.

So gh owns `gh:`, `gh-project:`, `gh-search:`, and github and git-sign add theirs to `gh:`.
git-sign owns `git-local:` and adds to `gh:` and `rad:`. The project's checkout gets the session's
default level for each kind: its GitHub and Radicle remotes, and its folder (`git-local:PATH`,
`PATH/*`).

Kinds that aren't written in the scope (`entry=False`) share a written kind's table
(`levels_of="gh"`) and say a target's level with a `level(sess, value)` hook:

- `gh-projects-of:OWNER` (`project list`, `project copy`'s target): the highest level among
  `gh-project:OWNER/*` patterns, which cover every board of the owner; `read` with `gh:OWNER` or with
  a single board of the owner.
- `gh-fork-into:OWNER`: the level of the `gh:OWNER` entry. The account's own login is in scope, as
  today, at `read`, so a fork into it asks unless `gh:LOGIN=maintain` or `allow` says otherwise.
- `gh-search-in`: `read` with a `gh-search:KIND` entry, else when the repository or owner it's
  limited to is in scope at `read` or higher. A `gh:X=none` entry doesn't open searches in X.
- `ssh:` (written, with its own `match`) also gets a `level` hook: the highest level among the
  entries that match `USER@HOST`; a connection (`@HOST`) takes the highest for that host.

### 3. What the names promise

Keep GitHub's names (`read`, `triage`, `write`, `maintain`), and document how apod's differ:

- apod's `write` never merges, never pushes the default branch, never pushes tags or deletes refs
  (GitHub's `write` does all of these). It's today's `dev`.
- apod's `maintain` merges through pull requests, with their checks and branch protection; it
  never pushes the default branch directly (`github:push default-branch` is in no level). On
  Radicle there is no merge: a delegate lands a patch by pushing the default branch, so
  `rad:maintain` has it.
- No `admin`.

Users already know what the names roughly mean; new names would need learning for little. The
differences are all in the safer direction.

The `maintain` actions come only from an entry written `=maintain`, never from a kind's default or a
`default_level`: every action in `gh:maintain` and `rad:maintain` above. The built-in `maintainer`
role therefore translates to `write` plus `allow` for them, as it is today (see Migration).

### 4. Actions without a target

**Rule:** a request with no target at all is allowed when the session has an entry of a kind that
lists the action, at a level that includes it. So these need no approval in almost every pod:
the project's remote is an entry. Classifiers keep using `unscoped` for "can't tell", so this
rule only covers requests that really touch nothing in particular.

| Action                                                                                                        | Where                                                                                                                |
| ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `gh search *`                                                                                                 | `gh-search:read`; `gh-search-in` targets take it (above).                                                            |
| `gh repo create`                                                                                              | `gh:maintain` on `gh:OWNER`. The new repository joins the scope at the session's default `gh:` level, like a remote. |
| `gh repo fork` (`gh-fork-into`)                                                                               | `gh:maintain`, bound to where the fork goes (above). The fork joins at the default `gh:` level.                      |
| `gh-projects-of` (`project list`, `copy`)                                                                     | `gh-project:` levels, through its `level` hook.                                                                      |
| `api GET /rate_limit`, `graphql query rateLimit`, `repo gitignore *`, `repo license *`, `github:fetch public` | `gh:read`, by the rule above.                                                                                        |
| `clipboard:image`, `notify:send`, `notify:urgent`                                                             | `allow`: per pod, not per target.                                                                                    |

### 5. Remotes and defaults

Remotes, the project folder, created repositories and forks get the session's default level for
their kind. The default is, kind by kind: `--role`'s bundle if given (it replaces the rest),
else the presets' `default_level`, then their role's bundle, else the profile's `default_level`,
then its role's bundle, else the kind's own (`gh` read, `gh-project` read, `gh-search` read, `rad`
read, `ssh` connect, `git-local` sign). `default_level` is the one part of today's role that survives: a table, nothing else.

A one-off approval that covers the next request too (`once_window`, a push's ref advertisement
then the push) puts the target in scope for those seconds at the default level, which is what
it does today with the role.

### 6. Migration

**Built-in roles** become bundles in `roles.toml`:

```toml
[roles.dev]
description = "push branches, open PRs and patches, comment and triage issues, sign commits"
default_level = { gh = "write", rad = "write", git-local = "sign" }
```

| Role         | `default_level`                              | `allow`                                                                               |
| ------------ | -------------------------------------------- | ------------------------------------------------------------------------------------- |
| `reader`     | gh read, rad read                            |                                                                                       |
| `dev`        | gh write, rad write, git-local sign          |                                                                                       |
| `maintainer` | gh write, rad write, git-local sign          | everything in `gh:maintain` and `rad:maintain` but `rad:push default-branch`          |
| `pm`         | gh triage, gh-project admin, rad read        |                                                                                       |
| `ops`        | gh none, rad none, ssh login, git-local sign | `github:fetch`, `github:fetch public`, `rad:fetch`, `git-sign:commit`, `git-sign:tag` |

`maintainer`'s `allow`, written out: `gh:pr merge`, `gh:pr review`, `gh:pr close`, `gh:pr reopen`,
`gh:pr update-branch`, `gh:run rerun`, `gh:run cancel`, `gh:workflow run`, `gh:repo fork`,
`gh:repo create`, `gh:graphql mutation mergePullRequest`,
`gh:graphql mutation addPullRequestReview`, `gh:graphql mutation submitPullRequestReview`,
`github:push tag`,
`github:push delete`, `rad:push tag`, `rad:push delete`: today's `maintainer` on every
repository in scope, unchanged. New setups write `=maintain` on the repositories instead.

`pm` is `gh-project:admin`, not `write`: it has `field-create`, `mark-template`, `copy` and
`link` today. `ops` keeps its "nothing on GitHub beyond fetching" with `none` and `allow`.

A bundle sets only the kinds it names; the others keep their own default. That is the fix for the
launch above: `dev` says nothing about boards, so `gh-project:` entries are `read`. Compared with
today, the bundles allow these on top, and nothing less:

- every role: the kinds it was silent on get their defaults: `gh-project:` read, `ssh:` connect,
  and, for `reader` and `pm`, `git-local:` sign (local-only repositories). These need an entry the
  user wrote, or the project folder for `git-local:`.
- `reader`: `graphql query organization`, `user`, `repositoryOwner` (on `gh:OWNER` entries).
- `dev`, `maintainer`: `triage`'s `issue edit`, `issue close`, `issue reopen`, `label create`,
  `label edit` and the `updateIssue` and label mutations, and the owner queries above.
- `pm`: `pr comment` (it had `addComment` already, which comments on PRs too), `label edit`.
- `ops`: searches, with a `gh-search:` entry.

A profile with no role and no `default_level` gets the kinds' defaults: `reader` plus `ssh`
connect and `git-local` sign.

**`--role X` and `role = "X"`** mean X's bundle: its `default_level` for entries without
`=LEVEL`, its `allow` and `deny` added to the profile's. Entries with their own level keep it. A
profile with both `role` and `default_level`: `default_level` wins, kind by kind.
`--role` stays as the command-line way to pick defaults (`--role reader` for a read-only look at a
project whose profile writes); no `--default-level` flag.

**Your roles in `~/.config/apod/roles.toml`** keep working, translated when they're read:

- one with a built-in's name: the built-in bundle, with its lists added to `allow` and its `deny`
  to `deny` (exactly what it adds today);
- a new one with `extends`: the bundle of the first role it extends, the rest of its actions
  added to `allow`;
- a new one without: for each kind that lists any of its actions, the highest level all of whose
  actions it has (`none` if it lacks some of the first; at most the highest level that can be a
  default, so not `maintain`); what's left goes to `allow`. Kinds it lists nothing of keep their
  default.

Each translation allows at least what the role does today. `apod broker roles` shows the result.

**Approvals saved "for the role"** (`grants.json` `roles`) keep applying, as `allow`, to sessions
launched with that role. New ones can't be saved that way (question 7).

**`apod migrate`** (profile files, `broker/profiles.py`) gains a pass that rewrites, after showing
the change and asking:

- `role = "X"` in a profile or preset: to X's `default_level`, plus its `allow` and `deny` merged
  into theirs;
- `~/.config/apod/roles.toml`: each role to the new form, as translated above;
- `grants.json` role approvals: into the `allow` of each profile and preset with that role. The
  old entries stay, so a launch with `--role X` keeps them.

Comments in the files are kept; the old text goes to `.old/` as `apod migrate` already does.
`apod migrate --layout` (`broker/migrate_layout.py`) copies `grants.json` as it does today; the
new `profiles` key is copied with it.

### 7. What users see

- **The reason line**, in the prompt, the TUI, the log and the agent's message, names the action,
  the entry and both levels:

  ```
  gh:issue edit needs triage on gh:logos-co/ecosystem, which is read (from gh:logos-co/*)
  out of this session's scope: gh:other/repo
  notify:send has no level: allow = ["notify:send"] in the profile allows it
  ```

  The event keeps `missing` (actions) and gains `raise`: `[[kind, value, level]]`, the entries
  approving would add.
- **Approving:** `once`, as today. `session` adds or raises the entries in `raise` for the session:
  an entry for the target itself (`gh:logos-co/ecosystem=triage`, never the whole `logos-co/*`) at
  the lowest level that lets the request through; `none` for a target the request only refers to.
  Actions with no level are added to the session's `allow`, as today. **`profile`** replaces
  `role`: the same entries and actions, saved in `grants.json` under `profiles.NAME`
  (`{"scope": [...], "allow": [...]}`) and applied to the profile's later launches. The broker
  never edits `profile.toml`. `apod broker allow ID --profile` (`--role` still works, with a note
  that it now saves for the profile); TUI key `p` (`r` too, for a while); `apod broker grants`
  lists both kinds, and `grants rm PROFILE ENTRY` removes one.
- **`apod broker levels [KIND]`** (new): each kind's levels, the actions each adds, which
  extension added them, the default, and which levels are explicit-only. `apod broker scopes`
  shows each kind's levels and default in a column.
- **`apod broker roles`**: the bundles, each kind's level and the `allow` it adds; the saved
  role approvals while there are any.
- **`apod broker extensions NAME`**: "What each role allows" becomes "Levels" (the levels of the
  kinds it owns, with all their actions) and "Its actions in other kinds' levels" (github: `gh`
  read: fetch, fetch public; write: push branch, push force; maintain: push tag, push delete).
- **`gh auth status`** and the agent's context (`Session.describe()`, which `register` sends as
  the context's head; apod's own `context+=` lines don't mention the role and don't change):

  ```
  apod broker: acting as fryorcraken (gh).
  Scope, each entry at its level (what it allows without asking; each level includes the ones before it):
    gh:fryorcraken/apod write, gh:logos-co/* read, gh-project:logos-co/* write, rad:z2Cj… write, git-local:/home/…/apod sign
  The levels in use:
    gh read: pr list, pr view, …; triage: + issue create, …; write: + pr create, …, github push branch, push force, git-sign commit, tag
    gh-project read: project list, …; write: + project item-add, …
  On anything in scope (allow): notify:send
  Approved by the user for this session: gh:logos-co/ecosystem=triage.
  ```

- **Launch warnings:** "ssh:nuc in scope, but role dev allows nothing of ssh" goes (the entry has
  a level now). New ones: an unknown level (the entry is dropped), and `maintain` as a default
  (an error).
- git-sign's context line says which entries sign, not "Role X doesn't sign".

## The extension API

Additive: `API_VERSION` stays 1, and extensions without levels work as before (their entries are
in scope, their actions come from `allow`, which old roles translate to).

- `scope_kind(..., levels=[(name, help, [actions])...], default=NAME, explicit=[names],
  levels_of=PREFIX, level=fn)`. `levels` in order; `default` is required with `levels`;
  `explicit` levels come only from entries that name them; `levels_of` shares another kind's
  table (for `entry=False` kinds); `level(sess, value)` returns a level name or `None` (out of
  scope), for kinds with their own `match` (without it, a target `match` says is in scope gets the
  session's default for the kind).
- `level_actions(prefix, level, actions)`: add actions to another extension's level.
- `Request.bind(action, targets)`: check that action against those targets only.
- `allowed(sess, action, target=None)`: with a target, at its level; without, anywhere in scope.
- `sess.scope` keeps its shape (`{prefix: [values]}`); `sess.level_of(target)` and
  `sess.levels` (`{prefix: {value: level}}`) are new. `add_scope(target, level=None)` (`None`: the
  default) raises an entry's level, never lowers it. `sess.role` is the bundle's name, or `""`.
- `evaluate` returns the same tuple; its reason text changes.

A third-party extension:

```python
from api import Provider, Request, check, level_actions, provider, rejected_msg, scope_kind

scope_kind("chat", provider="chat", help="chat:ROOM (a room on the team's chat server)",
           levels=[("read", "read the room", ["chat:read"]),
                   ("post", "post and edit your messages", ["chat:post", "chat:edit"])],
           default="read")
level_actions("gh", "triage", ["chat:link-issue"])   # optional: one of its actions on a GitHub repo


def handle(h, sess, method):
    req = Request("chat", f"post in {room}")
    req.actions, req.targets = ["chat:post"], [("chat", room)]   # as today
    ok, ev = check(sess, req)
```

`--scope chat:standup=post` then lets a pod post in `standup` without asking, and `chat:general`
(the default, `read`) lets it read there.

## Worked example

Before, the profile has `role = "dev"` and the preset `logos` sets no role:

```sh
apod fryorcraken --scope 'gh:logos-co/*' --scope 'gh-project:logos-co/*' --preset logos
# gh project item-list 18 --owner logos-co
#   waits: not allowed for role dev: gh:project item-list
```

After `apod migrate`, the profile has `default_level = { gh = "write", rad = "write", git-local
= "sign" }`. The same command line:

- `gh:logos-co/*` has no level: the profile's default for `gh`, `write`, as `dev` gives today.
- `gh-project:logos-co/*` has no level and `dev` says nothing about boards: `gh-project`'s default,
  `read`.
- `gh project item-list 18 --owner logos-co`: action `gh:project item-list`, target
  `gh-project:logos-co/18`, which is `read`. `gh-project:read` lists it: allowed, no approval.

What the job really wants, written out:

```sh
apod fryorcraken --scope 'gh:logos-co/*=read' --scope 'gh:logos-co/flywheels.logos.co=write' \
                 --scope 'gh-project:logos-co/*=write' --preset logos
```

Or in `presets/logos.toml`: `scope = ["gh:logos-co/*=read", "gh-project:logos-co/*=write"]`.
Then `gh project item-edit` goes through, `git push` to `flywheels.logos.co` goes through, and a
push or `gh pr create` in any other logos-co repo asks: "gh:pr create needs write on
gh:logos-co/rfp, which is read (from gh:logos-co/*)".

## The changes, in order

Each step keeps the tests passing and the docs that describe it up to date.

1. **Core: levels, beside roles** (`broker/core.py`, `broker/api.py`). `scope_kind`'s new
   arguments, `level_actions` (kept until the kind is declared), `=LEVEL` in `parse_scope`,
   `Session.levels` and `level_of`, max-of-matches, `none`, `Request.bind`, `evaluate`'s level
   path (the rule, bindings, targetless requests), `allowed(..., target)`. Roles still allow what
   they do: an action passes if the role or the level allows it. Saved sessions without `levels`
   read as defaults.
   *Tests* (with a test extension declaring a kind with levels): parsing (`=LEVEL`, a value with
   `=`, an unknown level), highest level wins, `none` is in scope but allows nothing, an action
   checked only against the kinds that list it, `bind` narrows, targetless requests, explicit
   levels refused as defaults, `level_actions` before the kind is declared and to a missing level,
   `HARD_DENY` and `deny` over levels, a saved session from before reloads.
2. **The shipped tables** (`extensions/gh.py`, `github.py`, `rad.py`, `ssh.py`, `git_sign.py`).
   The tables above; `level` hooks for `ssh:`, `gh-projects-of`, `gh-fork-into`, `gh-search-in`.
   *Tests:* every action in today's `roles.toml` is in a level or in this note's "in no level" or
   `allow` lists (a test reads `roles.toml`, so nothing is dropped silently); the level tables in
   `docs/reference.md` match the declared ones, as other doc tests do; the levels of the
   `entry=False` kinds.
3. **gh classifier** (`extensions/gh.py`). GraphQL bindings per field; `project copy` and
   `repo fork` bindings; `--add-project`/`--remove-project` titles to `gh-project:` targets; a URL
   selector replaces the clone's repository.
   *Tests* (fake gh): a GraphQL request across two repositories at different levels, item-add with
   an issue URL in a read-only repository, `--add-project` with a known and an unknown title, fork
   into the login and into an owner.
4. **Roles to bundles** (`roles.toml`, `broker/core.py`). The bundles; translation of your roles
   (built-in name, `extends`, plain); role approvals as `allow`; `register` sets the defaults from
   `--role`, presets, profile, kind; the role-action path in `evaluate` goes.
   *Tests:* each built-in role's bundle; for each built-in role, a set of requests decided as
   before, except the widenings listed in Migration, which are asserted as such; user roles of each
   form; a saved role approval still applies.
5. **Profiles and presets** (`broker/profiles.py`, `apod`, `apod-broker register`).
   `default_level` in profiles and presets, presets' `allow` and `deny`, collisions, `=LEVEL`
   checked in `scope` lists; apod passes presets' `default_level`, `allow` and `deny` to the broker
   (`--default-level KIND=LEVEL`, `--allow`, `--deny`, internal register options, as `--role` and
   `--scope` are today); `apod resume` records them with the rest. `apod new` asks for a bundle and
   writes `default_level`, and offers `=LEVEL` on the entries it adds.
   *Tests* (`ProfileLoaderTest`, `test_apod.py`): the merge and collision rules, `maintain` as a
   default refused, a preset's `allow`, the launch's register arguments.
6. **Approvals** (`broker/core.py` `decide`, `admin_call`, `broker/tui.py`). `raise` in events;
   session approvals add entries; profile approvals in `grants.json`; `allow ID --profile`
   (`--role` mapped); `grants` and `grants rm`; TUI keys and view 3.
   *Tests:* approve for the session, then the retry passes and the entry is the target's, not the
   pattern's; approve for the profile, then a new launch has it; `--role` still accepted;
   `grants rm`.
7. **What users see** (`broker/core.py`: `describe`, `roles_text`, `roles_plain`, `extension_info`,
   a new `levels_text`; `register`'s warnings; git-sign's context; `apod`'s usage and completion
   `_apod` for `apod broker levels`).
   *Tests:* the reason line, `gh auth status` text, `apod broker levels`, `roles`, `extensions gh`
   and `extensions github`, the new warnings.
8. **`apod migrate`** (`broker/profiles.py`, `apod`). The pass that rewrites roles, `roles.toml`
   and role approvals; `--layout` copies the `profiles` approvals.
   *Tests:* each built-in role in a profile and in a preset, a user `roles.toml` of each form,
   role approvals, comments kept, running it twice changes nothing; `migrate --layout` with
   `grants.json` holding both kinds.
9. **Docs.** `docs/guides.md` "Roles and scope" becomes "Scope and levels" (the tables, choosing
   levels, `default_level`, the bundles); `docs/reference.md` (`roles.toml`, "Scope kinds" with
   levels, presets' `default_level`, `allow` and `deny`); `EXTENSIONS.md` (the API above, the
   example); `SECURITY.md` (the checks are per target; `maintain` only from an entry that names
   it); the `roles.toml` header comment.
