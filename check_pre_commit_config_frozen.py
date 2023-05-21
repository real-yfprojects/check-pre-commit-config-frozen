"""Ensure correct `frozen: x.x.x` comments in `pre-commit-config.yaml`."""


from __future__ import annotations

import argparse
import enum
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import List, Mapping, Optional, Tuple, cast

from ruamel.yaml import YAML
from ruamel.yaml.util import load_yaml_guess_indent

# -- Git ---------------------------------------------------------------------

# The git commands in this section is partially sourced and modified from
# https://github.com/pre-commit/pre-commit/blob/main/pre_commit/git.py
# https://github.com/pre-commit/pre-commit/blob/main/pre_commit/util.py
#
# Original Copyright (c) 2014 pre-commit dev team: Anthony Sottile, Ken Struys


# prevents errors on windows
NO_FS_MONITOR = ("-c", "core.useBuiltinFSMonitor=false")


def no_git_env(_env: Mapping[str, str] | None = None) -> dict[str, str]:
    # Too many bugs dealing with environment variables and GIT:
    # https://github.com/pre-commit/pre-commit/issues/300
    # In git 2.6.3 (maybe others), git exports GIT_WORK_TREE while running
    # pre-commit hooks
    # In git 1.9.1 (maybe others), git exports GIT_DIR and GIT_INDEX_FILE
    # while running pre-commit hooks in submodules.
    # GIT_DIR: Causes git clone to clone wrong thing
    # GIT_INDEX_FILE: Causes 'error invalid object ...' during commit
    _env = _env if _env is not None else os.environ
    return {
        k: v
        for k, v in _env.items()
        if not k.startswith("GIT_")
        or k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        or k
        in {
            "GIT_EXEC_PATH",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_SSL_CAINFO",
            "GIT_SSL_NO_VERIFY",
            "GIT_CONFIG_COUNT",
            "GIT_HTTP_PROXY_AUTHMETHOD",
            "GIT_ALLOW_PROTOCOL",
            "GIT_ASKPASS",
        }
    }


def cmd_output(
    *cmd: str,
    check: bool = True,
    **kwargs,
) -> tuple[int, str, str]:
    for arg in ("stdin", "stdout", "stderr"):
        kwargs.setdefault(arg, subprocess.PIPE)

    proc = subprocess.Popen(cmd, text=True, **kwargs)
    stdout, stderr = proc.communicate()
    returncode = int(proc.returncode)

    if check and returncode:
        print(stdout)
        print(stderr)
        raise subprocess.CalledProcessError(returncode, cmd, stdout, stderr)

    return returncode, stdout, stderr


def init_repo(path: str, remote: str) -> None:
    if os.path.isdir(remote):
        remote = os.path.abspath(remote)

    git = ("git", *NO_FS_MONITOR)
    env = no_git_env()
    # avoid the user's template so that hooks do not recurse
    cmd_output(*git, "init", "--template=", path, env=env)
    cmd_output(*git, "remote", "add", "origin", remote, cwd=path, env=env)


@contextmanager
def tmp_repo(repo: str):
    with tempfile.TemporaryDirectory() as tmp:
        _git = ("git", *NO_FS_MONITOR, "-C", tmp)
        # init repo
        init_repo(tmp, repo)
        cmd_output(*_git, "config", "extensions.partialClone", "true")
        cmd_output(*_git, "config", "fetch.recurseSubmodules", "false")

        yield Path(tmp)


@lru_cache()
def get_tags(repo_url: str, hash: str) -> List[str]:
    with tmp_repo(repo_url) as repo_path:
        _git = ("git", *NO_FS_MONITOR, "-C", str(repo_path))

        # download rev
        # The --filter options makes use of git's partial clone feature.
        # It only fetches the commit history but not the commit contents.
        # Still it fetches all commits reachable from the given commit which is way more than we need
        cmd_output(
            *_git, "fetch", "origin", hash, "--quiet", "--filter=tree:0", "--tags"
        )
        # determine closest tag
        closest_tag = cmd_output(
            *_git, "describe", "FETCH_HEAD", "--abbrev=0", "--tags"
        )[1]
        closest_tag = closest_tag.strip()

        # determine tags
        out = cmd_output(*_git, "tag", "--points-at", f"refs/tags/{closest_tag}")[1]
        tags = out.splitlines()

    return tags


def get_hash_for(repo_url: str, rev: str) -> str:
    with tmp_repo(repo_url) as repo_path:
        _git = ("git", *NO_FS_MONITOR, "-C", repo_path)
        cmd_output(
            *_git, "fetch", "origin", rev, "--quiet", "--filter=tree:0", "--tags"
        )
        return cmd_output(*_git, "rev-parse", "FETCH_HEAD")[1].strip()


# -- Linter ------------------------------------------------------------------

# For the following code
# Copyright (c) 2023 real-yfprojects (github user)
# applies.

# sha-1 hashes consist of 160bit == 20 bytes
SHA1_LENGTH = 160

#: Minimum length of a abbrev. hex commit name (hash)
MIN_HASH_LENGTH = 7

# hex object names can also be abbreviated
regex_abbrev_hash = r"[\dabcdef]+"
pattern_abbrev_hash = re.compile(regex_abbrev_hash)


def is_hash(rev: str) -> bool:
    """
    Determine whether a revision is frozen.

    By definition a frozen revision is a complete hex object name. That is
    a SHA-1 hash. A shortened hex object name shouldn't be considered frozen
    since it might become ambiguous. Although you use SHA-1 hashes as tag names,
    the commits will take precedence over equally named tags.

    Parameters
    ----------
    rev : str
        The revision to check.

    Returns
    -------
    bool
        Whether the given revision can be considered frozen.
    """
    return bool(pattern_abbrev_hash.fullmatch(rev.lower()))


# A version can be frozen to every valid tag name or even any revision identifier
# as returned by `git describe`. The frozen comment can be followed by
# arbitrary text e.g. containing further explanation
regex_frozen_comment = r"#( frozen: (?P<rev>\S+))?(?P<note> .*)?"
pattern_frozen_comment = re.compile(regex_frozen_comment)

comment_template = "frozen: {rev}{note}"


def process_frozen_comment(comment: str) -> Optional[Tuple[str, str]]:
    """
    Check whether a comment specifies a frozen rev and extract its info.

    Parameters
    ----------
    comment : str
        The comment to process.

    Returns
    -------
    Optional[Tuple[str, str]]
        the revision and the arbitrary comment else None
    """
    match = pattern_frozen_comment.fullmatch(comment)
    if not match:
        return None

    return match["rev"], match["note"]


@enum.unique
class Rule(enum.Enum):
    #: Issued when a revision is not frozen although required
    FORCE_FREEZE = ("f", "Unfrozen revision: {rev}")

    #: Issued when a shortened hash is used although forbidden
    NO_ABBREV = ("a", "A abbreviated hash is specified for rev: {rev}")

    #: Issued when a revision is frozen although forbidden
    FORCE_UNFREEZE = ("u", "Frozen revision: {rev}")

    #: Issued when a revision is frozen but there is no comment stating the matching tag/revision.
    MISSING_FROZEN_COMMENT = ("m", "Missing comment specifying frozen version")

    #: Issued when there is a comment of form `frozen: xxx` although the rev isn't frozen
    EXCESS_FROZEN_COMMENT = (
        "e",
        "Although rev isn't frozen the comment says so: {comment}",
    )

    #: Issued when a revision is frozen and the comment doesn't mention the tag
    #: corresponding to the given commit hash
    CHECK_COMMENTED_TAG = ("t", "Tag doesn't match frozen rev: {frozen}")

    def __new__(cls, code, template):
        obj = object.__new__(cls)
        obj._value_ = code
        obj.code = code
        obj.template = template
        return obj


EXCLUSIVE_RULES = [(Rule.FORCE_FREEZE, Rule.FORCE_UNFREEZE)]


@dataclass()
class Complain:
    file: str
    line: int  # starting with 0
    column: int  # starting with 0
    type: Rule
    message: str
    fixable: bool
    fixed: bool = False


class Linter:
    def __init__(self, rules, fix) -> None:
        self.rules = rules
        self.fix = fix
        self._complains: List[Complain] = []
        self._current_file: Optional[str] = None
        self._current_complains: Optional[List[Complain]] = None

    @classmethod
    def get_tags(cls, repo_url: str, rev: str):
        return get_tags(repo_url, rev)

    @classmethod
    def select_best_tag(cls, repo_url: str, rev: str):
        tags = cls.get_tags(repo_url, rev)
        return min(
            filter(lambda s: "." in s, tags),
            key=len,
            default=None,
        ) or min(tags, key=len, default=None)

    def enabled(self, complain_or_rule):
        if isinstance(complain_or_rule, Rule):
            return complain_or_rule.value in self.rules
        if isinstance(complain_or_rule, Complain):
            return self.enabled(complain_or_rule.type)
        raise TypeError(f"Unsupported type {type(complain_or_rule)}")

    def should_fix(self, complain_or_rule):
        if isinstance(complain_or_rule, Rule):
            return self.enabled(complain_or_rule) and complain_or_rule.value in self.fix
        if isinstance(complain_or_rule, Complain):
            return complain_or_rule.fixable and self.should_fix(complain_or_rule.type)

    @contextmanager
    def file(self, file: str, complain_list: List[Complain]):
        if self._current_file:
            raise RuntimeError(f"Current file already set: {self._current_file}")

        self._current_file = file
        self._current_complains = complain_list
        yield
        self._complains.extend(self._current_complains)
        self._current_file = None

    def complain(self, type_: Rule, line: int, column: int, fixable: bool, **kwargs):
        if not self._current_file:
            raise RuntimeError("Current file not set.")

        msg = type_.template.format(**kwargs)
        c = Complain(self._current_file, line, column, type_, msg, fixable)

        if self.enabled(c):
            self._current_complains.append(c)
        return c

    def lint_repo(self, repo_yaml):
        repo_url = repo_yaml["repo"]
        rev = repo_yaml["rev"]
        line, column = repo_yaml.lc.value("rev")

        # parse comment
        comment_rev, comment_note = None, ""
        comments = repo_yaml.ca.items.get("rev") or [None] * 3
        comment_yaml = comments[2]
        if comment_yaml:
            match = process_frozen_comment(comment_yaml.value.strip())
            comment_rev, comment_note = match if match else (None, comment_yaml.value)
        comment_note = comment_note or ""

        # check rev
        ih = is_hash(rev)
        is_short_hash = ih and MIN_HASH_LENGTH <= len(rev) < SHA1_LENGTH / 4  # 40
        is_full_hash = len(rev) == SHA1_LENGTH / 4  # 40

        if is_short_hash:
            self.complain(Rule.NO_ABBREV, line, column, False, rev=rev)

        if is_short_hash or is_full_hash:
            # frozen hash
            comp = self.complain(
                Rule.FORCE_UNFREEZE, line, column, is_full_hash, rev=rev
            )

            if self.should_fix(comp):
                # select best tag
                tag = self.select_best_tag(repo_url, rev)

                if tag:
                    # adjust rev
                    repo_yaml["rev"] = tag
                    comp.fixed = True
                    is_short_hash = is_full_hash = False
                else:
                    # fixing failed
                    comp.fixable = False

        if is_short_hash or is_full_hash:
            comp = None
            if comment_rev is None:
                # no frozen: xxx comment
                comp = self.complain(
                    Rule.MISSING_FROZEN_COMMENT,
                    line,
                    comment_yaml.column if comment_yaml else column,
                    is_full_hash,  # need full_hash to generate comment
                    rev=rev,
                )
            elif is_full_hash and self.enabled(Rule.CHECK_COMMENTED_TAG):
                # Check the version specified in comment
                # need full_hash to identify commit

                # determine tags attached to closest commit with a tag
                tags = self.get_tags(repo_url, rev)

                if comment_rev not in tags:
                    # wrong version
                    comp = self.complain(
                        Rule.CHECK_COMMENTED_TAG,
                        line,
                        comment_yaml.column if comment_yaml else column,
                        True,
                        frozen=comment_rev,
                    )

            if comp and self.should_fix(comp):  # only true when fixable
                # select best tag
                tag = self.select_best_tag(repo_url, rev)

                if tag:
                    # adjust comment
                    repo_yaml.yaml_add_eol_comment(
                        comment_template.format(rev=tag, note=comment_note), "rev"
                    )
                    comp.fixed = True
                else:
                    # fixing failed
                    comp.fixable = False

        else:
            # unfrozen version
            comp = self.complain(Rule.FORCE_FREEZE, line, column, True, rev=rev)

            if self.should_fix(comp):
                # get full hash
                hash = get_hash_for(repo_url, rev)

                if hash:
                    # adjust rev
                    repo_yaml["rev"] = hash
                    # adjust comment
                    repo_yaml.yaml_add_eol_comment(
                        comment_template.format(rev=rev, note=comment_note), "rev"
                    )
                    comp.fixed = True
                else:
                    # fixing failed
                    comp.fixable = False

            if not self.enabled(comp):
                if comment_rev is not None:
                    # there is a frozen: xxx comment
                    comp = self.complain(
                        Rule.EXCESS_FROZEN_COMMENT,
                        line,
                        comment_yaml.column or column,
                        True,
                        comment=comment_yaml.value.strip(),
                    )

                    if self.should_fix(comp):
                        if comment_note:
                            # adjust comment
                            repo_yaml.yaml_add_eol_comment(comment_note, "rev")
                        else:
                            # remove comment
                            del repo_yaml.ca.items["rev"]
                        comp.fixed = True

    def run(self, file: str, content: str) -> Tuple[str, List[Complain]]:
        complains: List[Complain] = []
        with self.file(file, complains):
            # Load file
            yaml = YAML()
            config_yaml, ind, bsi = load_yaml_guess_indent(content)
            yaml.indent(mapping=bsi, sequence=ind, offset=bsi)

            # Lint
            for repo_yaml in config_yaml["repos"]:
                self.lint_repo(repo_yaml)

        stream = StringIO()
        yaml.dump(config_yaml, stream)
        return stream.getvalue(), complains


# 1. Differentiate frozen vs unfrozen version specifiers ✓
# 2. Retrieve version for given hash ✓
# 3. Detect frozen x.x.x comments ✓
# 4. Linting errors ✓
# 5. Fix linting errors where possible ✓
# 6. Define cmdline syntax


def get_parser():
    parser = argparse.ArgumentParser()

    fix_group = parser.add_mutually_exclusive_group()
    fix_group.add_argument("--fix", default="", dest="fix")
    fix_group.add_argument(
        "--fix-all",
        action="store_const",
        const="".join(r.value for r in Rule),
        dest="fix",
    )

    rule_group = parser.add_mutually_exclusive_group()
    rule_group.add_argument("--rules", default="", dest="rules")
    rule_group.add_argument(
        "--all-rules",
        action="store_const",
        const="".join(r.value for r in Rule),
        dest="rules",
    )

    parser.add_argument(
        "--print",
        action="store_true",
        help="Print fixed file contents to stdout instead of writing them back into the files.",
    )
    parser.add_argument("--quiet", action="store_true", help="Don't output anything")
    parser.add_argument(
        "--format",
        help="The output format for complains. Use python string formatting.",
        default="{fix}[{code}] {file}:{line}:{column} {msg}",
    )

    colour_group = parser.add_mutually_exclusive_group()
    colour_group.add_argument(
        "--colour",
        action="store_const",
        const=True,
        help="Colourful output",
        default=None,
    )
    colour_group.add_argument(
        "--no-colour",
        action="store_const",
        const=False,
        help="Colourful output [needs extra: colour]",
    )

    parser.add_argument("files", type=Path, nargs="+", metavar="file")

    return parser


def main():
    """The main entry point."""
    parser = get_parser()
    options = parser.parse_args()
    rules: str = options.rules

    # handle exclusive rules
    for exclusive_group in EXCLUSIVE_RULES:
        found = None
        for rule in exclusive_group:
            if rule.value in rules:
                if found:
                    parser.error(
                        f"Mutually exclusive rules `{found+rule.value}` specified"
                    )
                found = rule.value

    linter = Linter(rules, options.fix)

    for file in options.files:
        file = cast(Path, file)
        content = file.read_text()
        content, complains = linter.run(str(file), content)

        if options.print:
            print(content)  # noqa: T201
        elif options.fix:
            file.write_text(content)

        for comp in complains:
            print(  # noqa: T201
                options.format.format(
                    file=comp.file,
                    code=comp.type.code,  # type: ignore[attr-defined]
                    msg=comp.message,
                    line=comp.line + 1,
                    column=comp.column + 1,
                    fix="FIXED"
                    if comp.fixed
                    else "FIXABLE"
                    if comp.fixable
                    else "ERROR",
                )
            )


# TODO catch git errors
# TODO colour
# TODO speed

if __name__ == "__main__":
    main()
