"""Built-in role prompts, their overrides, and ejection.

Each role (worker, reviewer, triage) has a built-in template packaged
under ``corral/prompts/``. Placeholders are ``{name}``; the allowed set
is fixed, and a literal brace is written ``{{`` / ``}}``. Rendering is
strict: an unknown placeholder, or a known one with no value, is a
``PromptsError``.

An override replaces the built-in for one role. The first that exists
wins: the ``prompts.<role>`` setting (a path, relative to the ops root),
then ``<ops_root>/.corral/prompts/<role>.md``, then the built-in. An
override is validated with the same placeholder rule.
"""

import hashlib
import os
from importlib import resources

from corral import settings

#: The roles with a built-in template. Anything else is a PromptsError.
ROLES = ("worker", "reviewer", "triage")

#: The only placeholders a template may use.
ALLOWED = frozenset(
    {
        "issue",
        "title",
        "branch",
        "worktree",
        "checks.test",
        "overlay",
        "project_agents_md",
    }
)

#: The ejected override directory inside the ops root.
PROMPTS_DIR = os.path.join(".corral", "prompts")

#: The hash marker eject writes as the override's first line.
MARKER_PREFIX = "<!-- corral-prompt sha256:"
MARKER_SUFFIX = "-->"

settings.register(
    "prompts.worker", str, "", "override file for the worker prompt (relative to the ops root)"
)
settings.register(
    "prompts.reviewer",
    str,
    "",
    "override file for the reviewer prompt (relative to the ops root)",
)
settings.register(
    "prompts.triage", str, "", "override file for the triage prompt (relative to the ops root)"
)


class PromptsError(ValueError):
    """An unknown role, a bad placeholder, or an unusable override (exit 4)."""

    exit_code = 4


def _check_role(role):
    """The role, or a PromptsError naming what was asked for."""
    if role not in ROLES:
        raise PromptsError(f"unknown role {role!r}: expected one of {', '.join(ROLES)}")
    return role


def _builtin_text(role):
    """The packaged template for `role`. Tests patch this to move the built-in."""
    try:
        return resources.files("corral").joinpath(f"prompts/{role}.md").read_text("utf-8")
    except OSError as err:
        raise PromptsError(f"{role}: cannot read the built-in template: {err}") from None


def _scan(text):
    """Placeholder names in `text`, in order. ``{{`` and ``}}`` are escapes."""
    names = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "{":
            if text.startswith("{{", i):
                i += 2
                continue
            end = text.find("}", i + 1)
            if end < 0:
                break
            names.append(text[i + 1 : end])
            i = end + 1
        elif char == "}":
            i += 2 if text.startswith("}}", i) else 1
        else:
            i += 1
    return names


def known_placeholders(text):
    """The placeholder names in `text`. A pure scan, escapes excluded."""
    return set(_scan(text))


def _check_names(text, role):
    """The unknown-placeholder rule: every name in `text` must be allowed."""
    for name in _scan(text):
        if name not in ALLOWED:
            raise PromptsError(f"{role}: unknown placeholder {{{name}}}")


def _override_path(resolved, role, ops_root):
    """The setting's override path for `role`, or None when unset."""
    configured = resolved[f"prompts.{role}"]
    if not configured:
        return None
    if not isinstance(configured, str):
        raise PromptsError(f"{role}: prompts.{role} must be a path, not {configured!r}")
    if os.path.isabs(configured):
        return os.path.normpath(configured)
    if ops_root:
        return os.path.normpath(os.path.join(ops_root, configured))
    return os.path.normpath(os.path.abspath(configured))


def ejected_path(role, ops_root):
    """Where the ejected override for `role` lives. The ops root is required."""
    if not ops_root:
        raise PromptsError(f"cannot eject {role} without an ops root")
    return os.path.join(ops_root, PROMPTS_DIR, f"{role}.md")


def effective(role, ops_root=None):
    """The template for `role` and where it came from: setting, file or builtin.

    A setting that names a missing or unreadable file is a PromptsError.
    Placeholder validation happens in `render` and `check`, not here.
    """
    _check_role(role)
    resolved = settings.load(root=ops_root)
    override = _override_path(resolved, role, ops_root)
    if override is not None:
        try:
            with open(override, encoding="utf-8") as handle:
                return handle.read(), "setting"
        except OSError as err:
            raise PromptsError(f"{role}: cannot read the prompts override: {err}") from None
    if ops_root:
        path = ejected_path(role, ops_root)
        if os.path.lexists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    return handle.read(), "file"
            except OSError as err:
                raise PromptsError(f"{role}: cannot read the prompts override: {err}") from None
    return _builtin_text(role), "builtin"


def render(role, values, *, ops_root=None):
    """The effective template for `role` with every placeholder filled.

    A placeholder outside the allowed set, or an allowed one with no
    value, is a PromptsError naming the placeholder and the role. Extra
    values are fine.
    """
    _check_role(role)
    text, _ = effective(role, ops_root)
    _check_names(text, role)
    out = []
    i = 0
    while i < len(text):
        char = text[i]
        if text.startswith("{{", i) or text.startswith("}}", i):
            out.append(text[i])
            i += 2
            continue
        if char == "{":
            end = text.find("}", i + 1)
            if end >= 0:
                name = text[i + 1 : end]
                if name not in values:
                    raise PromptsError(f"{role}: no value for placeholder {{{name}}}")
                out.append(str(values[name]))
                i = end + 1
                continue
        out.append(char)
        i += 1
    return "".join(out)


def _marker(text):
    return f"{MARKER_PREFIX}{hashlib.sha256(text.encode('utf-8')).hexdigest()} {MARKER_SUFFIX}"


def eject(role, ops_root, *, force=False):
    """Copy the built-in template to the ejected path, with its hash first.

    Refuses to overwrite an existing file without `force`. Returns the path.
    """
    _check_role(role)
    path = ejected_path(role, ops_root)
    if os.path.exists(path) and not force:
        raise PromptsError(f"{path} exists: pass force to overwrite it")
    builtin = _builtin_text(role)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_marker(builtin) + "\n" + builtin)
    except OSError as err:
        raise PromptsError(f"{role}: cannot write the ejected prompt: {err}") from None
    return path


def _recorded(path):
    """The hash the ejected file records, or None when the marker is missing."""
    try:
        with open(path, encoding="utf-8") as handle:
            first = handle.readline().rstrip("\n")
    except OSError as err:
        raise PromptsError(f"cannot read the ejected prompt: {err}") from None
    if not first.startswith(MARKER_PREFIX) or not first.endswith(MARKER_SUFFIX):
        return None
    return first[len(MARKER_PREFIX) : -len(MARKER_SUFFIX)].strip()


def stale(role, ops_root):
    """True when the ejected file's hash differs from the current built-in.

    None when the role is not ejected or the marker is missing.
    """
    _check_role(role)
    if not ops_root:
        return None
    path = ejected_path(role, ops_root)
    if not os.path.isfile(path):
        return None
    recorded = _recorded(path)
    if recorded is None:
        return None
    current = hashlib.sha256(_builtin_text(role).encode("utf-8")).hexdigest()
    return recorded != current


class CheckResult:
    """What `check` found: invalid overrides by role, and stale ejections."""

    def __init__(self, invalid=None, stale_roles=None):
        self.invalid = dict(invalid or {})
        self.stale = list(stale_roles or [])

    @property
    def clean(self):
        """True when every override is valid and nothing is stale."""
        return not self.invalid and not self.stale


def check(ops_root=None):
    """Validate every override's placeholders and report stale ejections.

    An unreadable override raises PromptsError: the configuration itself
    is broken. A bad placeholder is collected under `invalid`.
    """
    invalid = {}
    stale_roles = []
    for role in ROLES:
        text, _ = effective(role, ops_root)
        try:
            _check_names(text, role)
        except PromptsError as err:
            invalid[role] = str(err)
            continue
        if stale(role, ops_root):
            stale_roles.append(role)
    return CheckResult(invalid, stale_roles)
