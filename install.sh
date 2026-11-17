#!/bin/sh
# Put a Project Tasks board into the repository you are standing in.
#
#   sh /path/to/project-tasks/install.sh --into ~/code/myapp
#
# That is the whole thing when you have a checkout: this script uses the board it
# is sitting in and touches the network for nothing. Without one:
#
#   sh install.sh --from git@host:org/project-tasks.git --into ~/code/myapp
#
# This script does two things and stops: it finds a copy of the board, and it
# hands that copy to `bin/install`, which decides what an install actually is.
# The PowerShell twin does the same, so there is one implementation of the
# install and two ways of reaching it rather than two installers to keep in step.
#
# POSIX sh on purpose. It has to run under dash on a Debian container and under
# whatever /bin/sh is on a Mac, and it is the first thing anybody runs.

set -eu

# Where the board comes from, in the order that actually works.
#
# 1. --from, when you give it: a directory or a URL.
# 2. The checkout this script is sitting in. `sh ~/src/project-tasks/install.sh`
#    needs no URL and no network at all, and installs exactly what is in that
#    checkout right now - which is the whole flow for a private repository.
# 3. That checkout's `origin`, so the address is whatever the repository says it
#    is rather than a second copy of it written down here to go stale.
# 4. PROJECT_TASKS_REPO, for a machine with no checkout to start from.
REF="${PROJECT_TASKS_REF:-main}"

# Resolved before the argument loop, because it is the directory this file is in
# and nothing can change that.
HERE="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

INTO="."
FROM=""
EXTRA=""

while [ $# -gt 0 ]; do
  case "$1" in
    --into) INTO="$2"; shift 2 ;;
    --from) FROM="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --check|--prune|--no-wire) EXTRA="$EXTRA $1"; shift ;;
    --prefix) EXTRA="$EXTRA --prefix $2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "install.sh: unknown option $1" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

have python3 || { echo "install.sh: python3 is required and is not on PATH" >&2; exit 1; }

# A local directory is used where it stands. Nothing is copied into a temporary
# place first, so `--from ../project-tasks` during development installs exactly
# what is in that checkout right now.
if [ -n "$FROM" ] && [ -d "$FROM" ]; then
  SOURCE="$FROM"
elif [ -z "$FROM" ] && [ -f "$HERE/bin/install" ]; then
  # This script is sitting in a board. Use it, and touch the network for nothing.
  SOURCE="$HERE"
else
  URL="$FROM"
  if [ -z "$URL" ] && [ -n "${PROJECT_TASKS_REPO:-}" ]; then
    URL="$PROJECT_TASKS_REPO"
  fi
  if [ -z "$URL" ] && have git; then
    # The address the repository itself knows, rather than a second copy of it
    # written down in this file to go stale the day it moves.
    URL="$(git -C "$HERE" remote get-url origin 2>/dev/null || true)"
  fi
  if [ -z "$URL" ]; then
    cat >&2 <<'MSG'
install.sh: nothing to install from.

This script is not inside a board checkout and no source was given. Any of these
works:

  sh install.sh --from /path/to/a/checkout    # a board you already have
  sh install.sh --from git@host:org/repo.git  # cloned with your own credentials
  sh install.sh --from https://host/org/repo  # public, fetched as a tarball
  PROJECT_TASKS_REPO=... sh install.sh
MSG
    exit 1
  fi

  TMP="$(mktemp -d)"
  # Removed however this exits, including the failure paths below: a half
  # unpacked board left in /tmp is the kind of thing that gets installed later
  # by somebody who does not know it is half unpacked.
  trap 'rm -rf "$TMP"' EXIT INT TERM

  case "$URL" in
    *.tar.gz|*.tgz) FETCH=tarball ;;
    # An scp-style or explicit git address, and anything private. `git clone`
    # is the only fetch here that authenticates: it uses the ssh key and the
    # credential helper already on the machine, which an unauthenticated
    # tarball request cannot, and a private repository answers that with a 404
    # that says nothing about why.
    git@*|ssh://*|git://*|*.git) FETCH=git ;;
    *) if have git; then FETCH=git; else FETCH=tarball; fi ;;
  esac

  if [ "$FETCH" = git ]; then
    echo "cloning $URL ($REF)"
    # --depth 1: the board's history is not what is being installed. The clone's
    # own .git is dropped, because what goes into somebody's repository is a
    # directory of files and not a repository inside a repository.
    git clone --quiet --depth 1 --branch "$REF" "$URL" "$TMP/board" \
      || git clone --quiet --depth 1 "$URL" "$TMP/board"
    rm -rf "$TMP/board/.git"
  else
    case "$URL" in
      *.tar.gz|*.tgz)   TARBALL="$URL" ;;
      *bitbucket.org/*) TARBALL="${URL%/}/get/$REF.tar.gz" ;;
      *)                TARBALL="${URL%/}/archive/refs/heads/$REF.tar.gz" ;;
    esac
    echo "fetching $TARBALL"
    if have curl; then
      curl -fsSL "$TARBALL" -o "$TMP/board.tar.gz"
    elif have wget; then
      wget -qO "$TMP/board.tar.gz" "$TARBALL"
    else
      echo "install.sh: neither curl nor wget is on PATH" >&2
      exit 1
    fi
    mkdir -p "$TMP/board"
    # --strip-components 1: every forge wraps the tree in one directory named
    # after the repository and the ref, and its name is not something this
    # script should have to predict.
    tar -xzf "$TMP/board.tar.gz" -C "$TMP/board" --strip-components 1
  fi
  SOURCE="$TMP/board"
fi

[ -f "$SOURCE/bin/install" ] || {
  echo "install.sh: $SOURCE does not look like a board (no bin/install)" >&2
  exit 1
}

# Run rather than exec: exec replaces this shell, and the EXIT trap that removes
# the unpacked tarball would never fire.
# shellcheck disable=SC2086
python3 "$SOURCE/bin/install" --source "$SOURCE" --into "$INTO" $EXTRA
exit $?
