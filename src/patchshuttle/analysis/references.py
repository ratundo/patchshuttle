"""Conservative syntactic symbol candidates, without binding or type inference."""

import ast

from patchshuttle._python_symbols import find_symbol_source
from patchshuttle.analysis.dependencies import import_targets, module_name


def dotted_expression(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def analyze_references(files, module, symbol, max_findings=2000):
    target = module + "." + symbol
    findings, trees = [], {}
    limited = False

    def record(kind, path, node=None, **details):
        nonlocal limited
        if len(findings) >= max_findings:
            limited = True
            return
        findings.append(
            dict(
                kind=kind,
                path=path,
                line=getattr(node, "lineno", None),
                column_utf8=getattr(node, "col_offset", None),
                **details,
            )
        )

    for path, text in sorted(files.items()):
        try:
            trees[path] = ast.parse(text, filename=path)
        except SyntaxError as exc:
            record("parse_error", path, error_line=exc.lineno)
    target_paths = [p for p in files if module_name(p) == module]
    status = "module_not_found_in_scan"
    if len(target_paths) > 1:
        status = "ambiguous_module"
    elif len(target_paths) == 1:
        path = target_paths[0]
        if path not in trees:
            status = "invalid_target_source"
        else:
            resolved = find_symbol_source(files[path], symbol, filename=path)
            status = (
                "resolved_declaration"
                if resolved is not None
                else "declaration_not_unique_or_missing"
            )
    for path, tree in sorted(trees.items()):
        current = module_name(path)
        if current is None:
            record("unknown_module_name", path)
            continue
        bindings = {}
        if current == module:
            root = symbol.split(".")[0]
            bindings[root] = {module + "." + root}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            targets = import_targets(node, current, path.endswith("/__init__.py"))
            for alias, (base, member) in zip(node.names, targets):
                if base is None:
                    record("relative_import_outside_root", path, node)
                    continue
                if member == "*":
                    record("star_import_unresolved", path, node, module=base)
                    continue
                qualified = base + "." + member if member else base
                if isinstance(node, ast.Import):
                    local = alias.asname or base.split(".")[0]
                    bound = base if alias.asname else local
                else:
                    local, bound = alias.asname or member, qualified
                bindings.setdefault(local, set()).add(bound)
                if target == qualified or target.startswith(qualified + "."):
                    record(
                        "import_prefix_candidate",
                        path,
                        node,
                        imported=qualified,
                        local_name=local,
                    )
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(
                node.ctx, ast.Load
            ):
                continue
            expression = dotted_expression(node)
            matched = False
            if expression is not None:
                head, *tail = expression.split(".")
                for prefix in sorted(bindings.get(head, ())):
                    qualified = ".".join([prefix, *tail])
                    if qualified == target or qualified.startswith(target + "."):
                        matched = True
                        record(
                            "syntactic_reference_candidate",
                            path,
                            node,
                            expression=expression,
                            qualified=qualified,
                        )
            if (
                isinstance(node, ast.Attribute)
                and node.attr == symbol.split(".")[-1]
                and not matched
            ):
                record(
                    "unresolved_attribute_candidate", path, node, attribute=node.attr
                )
    return dict(
        schema="patchshuttle.python_references.v1",
        target=target,
        target_status=status,
        findings=findings,
        findings_limit_reached=limited,
        binding_analysis="not_performed",
        type_inference="not_performed",
        limitations=[
            "scope shadowing and rebinding may produce false positives",
            "reexport chains, strings, dynamic access and instance types are not resolved",
            "conditional and nested imports are collected without control-flow analysis",
            "absence of candidates is not proof of safe refactoring",
        ],
    )
