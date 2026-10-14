"""Read supported literal CARLA map declarations without executing Scenic."""

import ast
import io
import re
import tokenize


def map_declaration(text):
    ignored = {tokenize.ENCODING, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT,
               tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT}
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type not in ignored]
    values = []
    depth = 1
    declarations = set()
    for index, token in enumerate(tokens[:-2]):
        if token.start[1] != 0:
            continue
        tail = tokens[index:]
        if token.string == "Town" and tail[1].string == "=" and tail[2].type == tokenize.STRING:
            if "Town" in declarations:
                raise ValueError("duplicate Town declaration")
            declarations.add("Town")
            values.append(ast.literal_eval(tail[2].string))
        elif token.string == "param" and len(tail) >= 4 and tail[1].string == "carla_map" and tail[2].string == "=" and tail[3].type == tokenize.STRING:
            if "carla_map" in declarations:
                raise ValueError("duplicate carla_map declaration")
            declarations.add("carla_map")
            values.append(ast.literal_eval(tail[3].string))
        if token.string == "param" and len(tail) >= 7 and [t.string for t in tail[:5]] == ["param", "map", "=", "localPath", "("] and tail[5].type == tokenize.STRING and tail[6].string == ")":
            if "map" in declarations:
                raise ValueError("duplicate map declaration")
            declarations.add("map")
            expression = ast.parse(tail[5].string, mode="eval").body
            if isinstance(expression, ast.JoinedStr):
                parts = expression.values
                if not (len(parts) == 3 and isinstance(parts[0], ast.Constant)
                        and parts[0].value == "../maps/"
                        and isinstance(parts[1], ast.FormattedValue)
                        and isinstance(parts[1].value, ast.Name) and parts[1].value.id == "Town"
                        and parts[1].conversion == -1 and parts[1].format_spec is None
                        and isinstance(parts[2], ast.Constant) and parts[2].value == ".xodr"):
                    raise ValueError("unsupported computed Scenic map path")
                # Legacy Scenic 2 path: the independent literal Town binds it.
                continue
            path = ast.literal_eval(expression)
            match = re.fullmatch(r"(\.\./){1,2}maps/(Town(?:0[1-7]|10HD))\.xodr", path)
            if not match:
                raise ValueError("literal Scenic map path must use ../maps or ../../maps")
            depth = path.count("../")
            values.append(match.group(2))
    if not values or any(not isinstance(v, str) or re.fullmatch(r"Town(?:0[1-7]|10HD)", v) is None for v in values) or len(set(values)) != 1:
        raise ValueError("Scenic artifact requires one consistent supported literal Town declaration")
    return values[0], depth
