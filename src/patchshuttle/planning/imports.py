"""Guarded top-level Python imports, preserving unrelated source verbatim."""

from __future__ import annotations

import ast

from patchshuttle._python_symbols import structural_hash
from patchshuttle.errors import PlanningErrorCode
from patchshuttle.planning.types import PlannedAction


def inspect_imports(text: str) -> tuple[ast.Module, list, str]:
    tree = ast.parse(text)
    compile(tree, "<imports>", "exec", dont_inherit=True)
    nodes = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    digest = structural_hash(ast.Module(body=nodes, type_ignores=[]))
    return tree, nodes, digest


def _key(node, alias):
    return (
        type(node).__name__,
        getattr(node, "module", None),
        getattr(node, "level", 0),
        alias.name,
        alias.asname,
    )


def _binding(node, alias):
    return alias.asname or (
        alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
    )


def _whole_lines(text, node):
    lines = text.splitlines(keepends=True)
    first, last = node.lineno - 1, node.end_lineno - 1
    prefix = lines[first].encode("utf-8")[: node.col_offset].decode("utf-8")
    suffix = lines[last].encode("utf-8")[node.end_col_offset :].decode("utf-8")
    if prefix.strip() or (suffix.strip() and not suffix.lstrip().startswith("#")):
        raise ValueError(
            "import edit requires a standalone statement, without semicolons"
        )
    return lines, first, last, suffix


def edit_import(text: str, statement: str, expected: str, *, remove: bool) -> str:
    tree, imports, digest = inspect_imports(text)
    if digest != expected.casefold():
        raise ValueError("imports AST guard does not match the current simulated file")
    request = ast.parse(statement)
    if (
        len(request.body) != 1
        or not isinstance(request.body[0], (ast.Import, ast.ImportFrom))
        or len(request.body[0].names) != 1
        or request.body[0].names[0].name == "*"
    ):
        raise ValueError(
            "statement must contain exactly one import with one non-star alias"
        )
    wanted = request.body[0]
    alias = wanted.names[0]
    key = _key(wanted, alias)
    matches = [
        node for node in imports for item in node.names if _key(node, item) == key
    ]
    if len(matches) > 1:
        raise ValueError("import is ambiguous: multiple matching aliases")
    if (matches and not remove) or (not matches and remove):
        return text
    if remove:
        node = matches[0]
        if len(node.names) != 1:
            raise ValueError(
                "removal from a multi-alias import requires a separately reviewed rewrite"
            )
        lines, first, last, suffix = _whole_lines(text, node)
        # Retain all comments, including comments inside parenthesized imports.
        import io
        import tokenize

        selected = "".join(lines[first : last + 1])
        comments = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(selected).readline)
            if token.type == tokenize.COMMENT
        ]
        updated = (
            "".join(lines[:first])
            + "".join(c + "\n" for c in comments)
            + "".join(lines[last + 1 :])
        )
    else:
        binding = _binding(wanted, alias)
        # Conservative across scopes: an added global must not shadow existing bindings.
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if any(
                    a.name == "*" or _binding(node, a) == binding for a in node.names
                ):
                    raise ValueError(
                        "new import conflicts with an existing import binding"
                    )
            if (
                (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Store)
                    and node.id == binding
                )
                or (
                    isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                    and node.name == binding
                )
                or (
                    isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar))
                    and node.name == binding
                )
                or (isinstance(node, ast.MatchMapping) and node.rest == binding)
                or (isinstance(node, ast.arg) and node.arg == binding)
            ):
                raise ValueError("new import conflicts with an existing Python binding")
        prefix = []
        for node in tree.body:
            if (
                not prefix
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ) or (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
                prefix.append(node)
            else:
                break
        lines = text.splitlines(keepends=True)
        if prefix:
            _whole_lines(text, prefix[-1])
            index = prefix[-1].end_lineno
        elif tree.body:
            index = tree.body[0].lineno - 1
            if isinstance(
                tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                index = min(
                    [index, *(d.lineno - 1 for d in tree.body[0].decorator_list)]
                )
        else:
            index = len(lines)
        before = "".join(lines[:index])
        if before and not before.endswith("\n"):
            before += "\n"
        updated = before + ast.unparse(wanted) + "\n" + "".join(lines[index:])
    compile(updated, "<imports>", "exec", dont_inherit=True)
    return updated


def plan_import_edit(planner, action, *, item_id):
    parameters = action.parameters
    path = planner.policy.normalize(parameters.path)
    target = planner.policy.resolve(path, allow_missing=True)
    state = planner._get_file(path, target, item_id=item_id)
    try:
        if path.suffix.casefold() != ".py":
            raise ValueError("structural import edits require a .py file")
        updated = edit_import(
            state.text,
            parameters.statement,
            parameters.expected_imports_ast_sha256,
            remove=action.name == "remove_import",
        )
    except (SyntaxError, ValueError) as exc:
        raise planner._error(
            PlanningErrorCode.PYTHON_SOURCE_INVALID,
            str(exc),
            item_id,
            path=path.as_posix(),
        ) from exc
    disposition = planner._update_state(state, updated, item_id=item_id)
    planner.action_plans.append(
        PlannedAction(
            id=item_id, name=action.name, disposition=disposition, paths=(path,)
        )
    )
