"""Parse a `.metta` fixture file into the atoms that represent graph data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Union

Atom = Union[str, "SExpr"]


@dataclass(frozen=True)
class SExpr:
    """A parsed S-expression: a symbol head plus its arguments, in order."""

    head: str
    args: tuple

    def __repr__(self) -> str:
        return f"({self.head} {' '.join(map(_repr_arg, self.args))})"


def _repr_arg(a: Atom) -> str:
    return repr(a) if isinstance(a, str) else repr(a)


class MettaSyntaxError(ValueError):
    """The file contains something this parser doesn't recognize."""


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> Iterator[str]:
    """Yields '(', ')', quoted strings (with the quotes kept), and bare
    symbols. Comments (`;;` to end of line) are stripped first."""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == ";":
            # a comment runs to end of line
            j = text.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c in "()":
            yield c
            i += 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1  # skip an escaped character
                j += 1
            if j >= n:
                raise MettaSyntaxError(f"unterminated string starting at offset {i}")
            yield text[i : j + 1]
            i = j + 1
            continue
        j = i
        while j < n and text[j] not in " \t\r\n()":
            j += 1
        yield text[i:j]
        i = j


# ---------------------------------------------------------------------------
# Parser: tokens -> a tree of Atom (str | SExpr)
# ---------------------------------------------------------------------------


def _parse_one(tokens: List[str], pos: int) -> tuple:
    """Parses one atom starting at tokens[pos]. Returns (atom, next_pos)."""
    tok = tokens[pos]
    if tok == "(":
        head = None
        args = []
        pos += 1
        while pos < len(tokens) and tokens[pos] != ")":
            atom, pos = _parse_one(tokens, pos)
            if head is None:
                head = atom if isinstance(atom, str) else None
                if head is None:
                    raise MettaSyntaxError("expression head is not a plain symbol")
            else:
                args.append(atom)
        if pos >= len(tokens):
            raise MettaSyntaxError("unclosed '('")
        return SExpr(head=head, args=tuple(args)), pos + 1
    if tok == ")":
        raise MettaSyntaxError("unexpected ')'")
    if tok.startswith('"'):
        return _unquote(tok), pos + 1
    return tok, pos + 1  # a bare symbol: $var, a number, mm2-exec, &mork, ...


def _unquote(tok: str) -> str:
    """Strips the surrounding quotes and resolves backslash escapes."""
    inner = tok[1:-1]
    return inner.replace('\\"', '"').replace("\\\\", "\\")


def parse_top_level_forms(text: str) -> List[SExpr]:
    """Every top-level `(...)` form in the file, in order.

    Each `!` prefix (MeTTa's "execute this" marker) is stripped before the
    form itself is tokenized -- it isn't part of the S-expression grammar.
    """
    # Strip a leading '!' from each command; 
    # inside the expression tree.
    cleaned = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "!" and i + 1 < n and text[i + 1] == "(":
            i += 1
            continue
        cleaned.append(text[i])
        i += 1
    tokens = list(_tokenize("".join(cleaned)))

    forms: List[SExpr] = []
    pos = 0
    while pos < len(tokens):
        atom, pos = _parse_one(tokens, pos)
        if isinstance(atom, SExpr):
            forms.append(atom)
        # a bare top-level symbol (shouldn't normally occur) is silently
        # ignored -- it carries no graph information either way
    return forms


# ---------------------------------------------------------------------------
# Extracting graph atoms specifically from `add-atom` forms
# ---------------------------------------------------------------------------

GRAPH_ATOM_HEADS = frozenset({"node", "field", "edge", "rev-edge", "efield", "layer"})


@dataclass(frozen=True)
class ParseResult:
    graph_atoms: List[SExpr]
    skipped_forms: List[SExpr]  # mm2-exec, match, or any unrecognized head
    unrecognized_add_atom_heads: List[str]  # add-atom bodies with an unknown head


def extract_graph_atoms(text: str) -> ParseResult:
    """Walks every top-level form and pulls out the atoms that represent
    graph data -- the argument of every `(add-atom &mork <ATOM>)` call whose
    head is one of GRAPH_ATOM_HEADS. Everything else (`mm2-exec`, `match`
    query blocks, or an add-atom with an unrecognized head) is reported
    separately rather than silently dropped, so a genuinely new atom type
    added by a future rules module doesn't just vanish without a trace.
    """
    graph_atoms: List[SExpr] = []
    skipped: List[SExpr] = []
    unrecognized_heads: List[str] = []

    for form in parse_top_level_forms(text):
        if form.head != "add-atom":
            skipped.append(form)
            continue
        if len(form.args) != 2:
            raise MettaSyntaxError(f"add-atom with unexpected arity: {form!r}")
        _space, inner = form.args
        if not isinstance(inner, SExpr):
            raise MettaSyntaxError(f"add-atom argument is not an expression: {form!r}")
        if inner.head in GRAPH_ATOM_HEADS:
            graph_atoms.append(inner)
        else:
            unrecognized_heads.append(inner.head)
            skipped.append(form)

    return ParseResult(
        graph_atoms=graph_atoms,
        skipped_forms=skipped,
        unrecognized_add_atom_heads=unrecognized_heads,
    )