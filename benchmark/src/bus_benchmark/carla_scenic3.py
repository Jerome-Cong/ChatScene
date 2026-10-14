"""Ego-only normalization of Scenic 3 source; no execution or semantic repair."""

import ast
import io
import tokenize

from .errors import ValidationError


ARTIFACT_TYPE = "carla_scenic3_v0.1"
_TARGETS = {"blueprint": "vehicle.chevrolet.impala", "length": 5.33, "width": 2.1}
_LITERALS = {"blueprint": '"vehicle.chevrolet.impala"', "length": "5.33", "width": "2.1"}
_IGNORED = {tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT,
            tokenize.ENDMARKER, tokenize.ENCODING}


def finalize(raw):
    try:
        text = raw.decode("utf-8")
        lines = text.splitlines(keepends=True)
        tokens = [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type not in _IGNORED]
    except (UnicodeError, tokenize.TokenError, IndentationError) as exc:
        raise ValidationError("Scenic 3 source is not readable") from exc
    bindings = [i for i, token in enumerate(tokens[:-1])
                if token.type == tokenize.NAME and token.string == "ego" and tokens[i + 1].string == "="]
    if len(bindings) != 1:
        raise ValidationError("Scenic 3 artifact requires exactly one ego binding")
    starts = [i for i, token in enumerate(tokens[:-3])
              if token.string == "ego" and token.start[1] == 0
              and [t.string for t in tokens[i + 1:i + 4]] == ["=", "new", "Car"]]
    if len(starts) != 1:
        raise ValidationError("Scenic 3 artifact requires one top-level ego = new Car declaration")
    start = starts[0]
    car = tokens[start + 3]
    first_line = car.start[0]
    last_line = first_line
    while last_line < len(lines):
        line = lines[last_line]
        if line.strip() and len(line) - len(line.lstrip(" \t")) == 0:
            break
        last_line += 1
    body = [t for t in tokens[start + 4:] if t.start[0] <= last_line]
    if any(t.type == tokenize.OP and t.string == ";" for t in body):
        raise ValidationError("Scenic 3 ego declaration contains multiple statements")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def offset(position):
        return offsets[position[0] - 1] + position[1]

    edits = []
    seen = set()
    changes = []
    depth = 0
    index = 0
    while index < len(body):
        token = body[index]
        if token.string in ("(", "[", "{"):
            depth += 1
        elif token.string in (")", "]", "}"):
            depth -= 1
        if depth == 0 and token.string in (";", "="):
            raise ValidationError("Scenic 3 ego declaration contains multiple statements")
        if depth == 0 and token.string == "with" and index + 1 < len(body):
            name = body[index + 1].string
            if name in _TARGETS:
                if name in seen:
                    raise ValidationError("Scenic 3 ego has duplicate {} clauses".format(name))
                seen.add(name)
                end = index + 2
                local_depth = 0
                while end < len(body):
                    part = body[end]
                    if local_depth == 0 and part.string in ("=", ";", "with"):
                        raise ValidationError("Scenic 3 ego property contains another statement or specifier")
                    if local_depth == 0 and part.string == ",":
                        break
                    if part.string in ("(", "[", "{"):
                        local_depth += 1
                    elif part.string in (")", "]", "}"):
                        local_depth -= 1
                    end += 1
                if end == index + 2 or local_depth != 0:
                    raise ValidationError("Scenic 3 ego has an incomplete {} expression".format(name))
                begin_offset = offset(body[index + 2].start)
                end_offset = offset(body[end - 1].end)
                expression = text[begin_offset:end_offset]
                try:
                    ast.parse(expression, mode="eval")
                except SyntaxError as exc:
                    raise ValidationError("Scenic 3 ego property must be a bounded Python expression") from exc
                try:
                    current = ast.literal_eval(expression)
                except (ValueError, SyntaxError):
                    current = None
                if type(current) not in (str, float, int) or current != _TARGETS[name]:
                    edits.append((begin_offset, end_offset, _LITERALS[name]))
                    changes.append("ego_" + name)
                index = end
                continue
        index += 1
    if depth != 0:
        raise ValidationError("Scenic 3 ego declaration has unbalanced expressions")
    missing = [name for name in _TARGETS if name not in seen]
    if missing:
        clauses = ", ".join("with {} {}".format(name, _LITERALS[name]) for name in missing)
        # Insert before existing specifiers, leaving MODEL assignments and all
        # NPC references intact even when the native header shares one variable.
        suffix = "," if body and body[0].string != "," else ""
        edits.append((offset(car.end), offset(car.end), " " + clauses + suffix))
        changes.extend("ego_" + name for name in missing)
    for begin, end, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[end:]
    return text.encode("utf-8"), tuple(changes)


def verify(raw, final):
    expected, _ = finalize(raw)
    if expected != final:
        raise ValidationError("Scenic 3 final artifact differs from ego-only normalization")
