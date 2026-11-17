<#
.SYNOPSIS
Put a Project Tasks board into the repository you are standing in.

.DESCRIPTION
The Windows twin of install.sh. Both do the same two things and stop: find a
copy of the board, and hand it to bin/install, which decides what an install
actually is. There is one implementation of the install and two ways of
reaching it, rather than two installers that drift into two products.

.EXAMPLE
# from a checkout you already have, which needs no network at all
.\install.ps1 -Into C:\code\myapp

.EXAMPLE
# from a repository, cloned with the credentials already on this machine
.\install.ps1 -From git@host:org/project-tasks.git -Into C:\code\myapp

.EXAMPLE
# what would change, changing nothing
.\install.ps1 -From C:\src\project-tasks -Check
#>

[CmdletBinding()]
param(
    [string]$Into = ".",
    # A URL or a local directory. A local directory is installed from where it
    # stands, so a development checkout installs exactly what is in it now.
    [string]$From = "",
    [string]$Ref = "main",
    [string]$Prefix = "",
    [switch]$Check,
    [switch]$Prune,
    [switch]$NoWire
)

$ErrorActionPreference = "Stop"

# Where the board comes from, in the order that actually works.
#
# 1. -From, when you give it: a directory or a URL.
# 2. The checkout this script is sitting in. `.\install.ps1 -Into C:\code\app`
#    needs no URL and no network at all, which is the whole flow for a private
#    repository.
# 3. That checkout's origin, so the address is whatever the repository says it
#    is rather than a second copy of it written down here to go stale.
# 4. $env:PROJECT_TASKS_REPO, for a machine with no checkout to start from.
if ($env:PROJECT_TASKS_REF) { $Ref = $env:PROJECT_TASKS_REF }
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Find-Python {
    # The py launcher first: it is what a Windows python install provides, and
    # it picks a real Python 3 rather than the App Execution Alias stub that
    # `python` can be, which opens the Store instead of running anything.
    #
    # Returned as an explicit Exe/Args pair and never as a sliced array.
    # PowerShell's `1..0` is not an empty range - it counts backwards, so
    # `$a[1..($a.Count-1)]` on a one-element array yields `$null, $a[0]`, which
    # would put a null and a duplicate in the middle of the command line.
    foreach ($candidate in @(
            @{ Name = "py";      Args = @("-3") },
            @{ Name = "python3"; Args = @() },
            @{ Name = "python";  Args = @() })) {
        $exe = Get-Command $candidate.Name -ErrorAction SilentlyContinue
        if ($exe) { return @{ Exe = $exe.Source; Args = $candidate.Args } }
    }
    throw "python3 is required and was not found. Install it from python.org or the Microsoft Store."
}

$python = Find-Python
$temp = $null

try {
    if ($From -and (Test-Path -PathType Container $From)) {
        $source = (Resolve-Path $From).Path
    }
    elseif (-not $From -and (Test-Path (Join-Path $Here "bin/install"))) {
        # This script is sitting in a board. Use it, and touch the network for
        # nothing.
        $source = $Here
    }
    else {
        $url = $From
        if (-not $url -and $env:PROJECT_TASKS_REPO) { $url = $env:PROJECT_TASKS_REPO }
        if (-not $url -and (Get-Command git -ErrorAction SilentlyContinue)) {
            # The address the repository itself knows, rather than a second copy
            # of it written down in this file to go stale the day it moves.
            $url = (& git -C $Here remote get-url origin 2>$null)
        }
        if (-not $url) {
            Write-Host @'
install.ps1: nothing to install from.

This script is not inside a board checkout and no source was given. Any of these
works:

  .\install.ps1 -From C:\path	o\checkout     # a board you already have
  .\install.ps1 -From git@host:org/repo.git     # cloned with your credentials
  .\install.ps1 -From https://host/org/repo     # public, fetched as a tarball
  $env:PROJECT_TASKS_REPO = "..."
'@
            exit 1
        }

        $temp = Join-Path ([System.IO.Path]::GetTempPath()) ("project-tasks-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $temp -Force | Out-Null
        $unpacked = Join-Path $temp "board"

        # git clone is the only fetch here that authenticates: it uses the ssh
        # key and the credential helper already on the machine, which an
        # unauthenticated tarball request cannot -- and a private repository
        # answers that with a 404 that says nothing about why.
        $useGit = ($url -match '^(git@|ssh://|git://)' -or $url -match '\.git$' -or
                   ($url -notmatch '\.(tar\.gz|tgz)$' -and
                    (Get-Command git -ErrorAction SilentlyContinue)))

        if ($useGit) {
            Write-Host "cloning $url ($Ref)"
            # --depth 1: the board's history is not what is being installed. The
            # clone's own .git is dropped, because what goes into somebody's
            # repository is a directory of files, not a repository inside one.
            & git clone --quiet --depth 1 --branch $Ref $url $unpacked
            if ($LASTEXITCODE -ne 0) {
                & git clone --quiet --depth 1 $url $unpacked
                if ($LASTEXITCODE -ne 0) { throw "could not clone $url" }
            }
            Remove-Item -Recurse -Force (Join-Path $unpacked ".git") -ErrorAction SilentlyContinue
        }
        else {
            $tarball = switch -Regex ($url) {
                '\.(tar\.gz|tgz)$' { $url; break }
                'bitbucket\.org'    { "$($url.TrimEnd('/'))/get/$Ref.tar.gz"; break }
                default             { "$($url.TrimEnd('/'))/archive/refs/heads/$Ref.tar.gz" }
            }
            $archive = Join-Path $temp "board.tar.gz"
            Write-Host "fetching $tarball"
            Invoke-WebRequest -Uri $tarball -OutFile $archive -UseBasicParsing
            New-Item -ItemType Directory -Path $unpacked -Force | Out-Null
            # tar has shipped with Windows since 1803. --strip-components 1
            # because every forge wraps the tree in one directory named after
            # the repository and the ref, and its name is not something this
            # script should have to predict.
            & tar -xzf $archive -C $unpacked --strip-components 1
            if ($LASTEXITCODE -ne 0) { throw "could not unpack $archive" }
        }
        $source = $unpacked
    }

    if (-not (Test-Path (Join-Path $source "bin/install"))) {
        throw "$source does not look like a board (no bin/install)"
    }

    $arguments = @()
    $arguments += $python.Args
    $arguments += @((Join-Path $source "bin/install"), "--source", $source, "--into", $Into)
    if ($Check)  { $arguments += "--check" }
    if ($Prune)  { $arguments += "--prune" }
    if ($NoWire) { $arguments += "--no-wire" }
    if ($Prefix) { $arguments += @("--prefix", $Prefix) }

    # Splatted with @, so each element becomes its own argument. Passed as one
    # array in parentheses PowerShell would hand the program a single
    # space-joined string, and every path with a space in it would break.
    & $python.Exe @arguments
    exit $LASTEXITCODE
}
finally {
    # Whatever happened, including a failed download: a half unpacked board left
    # in the temp directory is the kind of thing that gets installed later by
    # somebody who does not know it is half unpacked.
    if ($temp -and (Test-Path $temp)) {
        Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
    }
}
