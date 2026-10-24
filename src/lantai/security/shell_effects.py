# -*- coding: utf-8 -*-
"""Conservative shell effect extraction used by permission decisions.

The permission layer needs to distinguish a path which is merely read from a
path which is modified. A flat "all paths in a command" matcher cannot do
that: ``ls /Applications 2>/dev/null`` reads one system directory and writes
only to the null device. This module models a small, auditable set of common
file-mutating commands and output redirections. Unrecognised commands remain
subject to the caller's normal prompt policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shlex


_CONTROL_OPERATORS = {"|", "||", "&&", ";", "&"}
_OUTPUT_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
_INPUT_REDIRECTS = {"<", "<<", "<<<"}
_READ_WRITE_REDIRECTS = {"<>"}
_SAFE_OUTPUT_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


@dataclass(frozen=True)
class ShellEffects:
    """Paths whose role can be established from a shell command."""

    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()


def _tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return shlex.split(command, posix=True) if command.strip() else []


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _CONTROL_OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _dedupe(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _is_path_operand(value: str) -> bool:
    return bool(value) and value != "-" and not value.startswith("-")


def _command_effects(segment: list[str]) -> tuple[list[str], list[str]]:
    if not segment:
        return [], []

    args: list[str] = []
    reads: list[str] = []
    writes: list[str] = []
    i = 0
    while i < len(segment):
        token = segment[i]
        if (
            token in _OUTPUT_REDIRECTS
            or token in _INPUT_REDIRECTS
            or token in _READ_WRITE_REDIRECTS
        ):
            if args and args[-1].isdigit():
                args.pop()
            if i + 1 < len(segment):
                target = segment[i + 1]
                if token in _OUTPUT_REDIRECTS:
                    if target not in _SAFE_OUTPUT_SINKS:
                        writes.append(target)
                elif token in _READ_WRITE_REDIRECTS:
                    reads.append(target)
                    if target not in _SAFE_OUTPUT_SINKS:
                        writes.append(target)
                else:
                    reads.append(target)
                i += 2
                continue
        args.append(token)
        i += 1

    if not args:
        return reads, writes
    head = Path(args[0]).name
    operands = [arg for arg in args[1:] if _is_path_operand(arg)]

    if head in {"cp", "install"} and operands:
        reads.extend(operands[:-1])
        writes.append(operands[-1])
    elif head == "mv" and operands:
        # Moving mutates both the source directory entry and destination.
        writes.extend(operands)
    elif head in {"rm", "rmdir", "unlink", "mkdir", "touch", "truncate"}:
        writes.extend(operands)
    elif head in {"chmod", "chown"} and len(operands) >= 2:
        writes.extend(operands[1:])
    elif head == "tee":
        writes.extend(operands)
    elif head == "sed" and any(
        option == "-i" or option.startswith("-i") or option == "--in-place"
        for option in args[1:]
    ):
        if operands:
            writes.append(operands[-1])
    else:
        reads.extend(operands)
    return reads, writes


def extract_shell_effects(command: str) -> ShellEffects:
    """Return stable, de-duplicated read and write path operands."""

    reads: list[str] = []
    writes: list[str] = []
    for segment in _segments(_tokens(command)):
        segment_reads, segment_writes = _command_effects(segment)
        reads.extend(segment_reads)
        writes.extend(segment_writes)
    return ShellEffects(read_paths=_dedupe(reads), write_paths=_dedupe(writes))
