"""Static module candidates only; no imports, execution or runtime resolution."""

import ast
from pathlib import PurePosixPath


def cycle_groups(graph):
    """Iterative Kosaraju traversal avoids recursion and repeated graph searches."""
    visited, order = set(), []
    reverse = {node: set() for node in graph}
    for node, targets in graph.items():
        for target in targets:
            reverse[target].add(node)
    for start in sorted(graph):
        if start in visited:
            continue
        visited.add(start)
        stack = [(start, iter(sorted(graph[start])))]
        while stack:
            node, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                order.append(node)
                stack.pop()
            elif target not in visited:
                visited.add(target)
                stack.append((target, iter(sorted(graph[target]))))
    seen, groups = set(), []
    for start in reversed(order):
        if start in seen:
            continue
        seen.add(start)
        group, pending = set(), [start]
        while pending:
            node = pending.pop()
            group.add(node)
            for target in reverse[node] - seen:
                seen.add(target)
                pending.append(target)
        if len(group) > 1 or start in graph[start]:
            groups.append(sorted(group))
    return sorted(groups)


def module_name(path):
    file = PurePosixPath(path)
    parts = file.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts and all(p.isidentifier() for p in parts) else None


def import_targets(node, module, package_file):
    """Return module spellings; from-imported names may be symbols or submodules."""
    if isinstance(node, ast.Import):
        return [(alias.name, None) for alias in node.names]
    package = module.split(".") if package_file else module.split(".")[:-1]
    if node.level > len(package):
        return [(None, None)]
    base = package[: len(package) - node.level + 1] if node.level else []
    if node.module:
        base.extend(node.module.split("."))
    name = ".".join(base)
    return [(name, alias.name) for alias in node.names]


def analyze_dependencies(files, max_findings=2000):
    """Analyze paths relative to one explicitly selected import root."""
    findings = []
    limited = False

    def record(**item):
        nonlocal limited
        if len(findings) < max_findings:
            findings.append(item)
        else:
            limited = True

    paths_by_module = {}
    for path in sorted(files):
        name = module_name(path)
        if name is None:
            record(kind="unknown_module_name", path=path)
        else:
            paths_by_module.setdefault(name, []).append(path)
    modules = {
        name: paths[0] for name, paths in paths_by_module.items() if len(paths) == 1
    }
    for name, paths in sorted(paths_by_module.items()):
        if len(paths) > 1:
            record(kind="ambiguous_module", module=name, paths=paths)
    graph = {name: set() for name in modules}
    for module, path in sorted(modules.items()):
        try:
            tree = ast.parse(files[path], filename=path)
        except SyntaxError as exc:
            record(kind="parse_error", path=path, line=exc.lineno)
            continue
        dynamic_names = {"__import__", "import_module"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "importlib":
                dynamic_names.update(
                    a.asname or a.name for a in node.names if a.name == "import_module"
                )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id in dynamic_names
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                record(kind="dynamic_import_hint", path=path, line=node.lineno)
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for base, member in import_targets(
                node, module, path.endswith("/__init__.py")
            ):
                if base is None:
                    record(
                        kind="relative_import_outside_root", path=path, line=node.lineno
                    )
                    continue
                requested = base
                candidate = base + "." + member if member else base
                if member != "*" and candidate in modules:
                    requested = candidate
                if member == "*":
                    record(kind="star_import", path=path, line=node.lineno, target=base)
                if requested not in modules:
                    record(
                        kind="external_or_unresolved",
                        path=path,
                        line=node.lineno,
                        target=requested,
                    )
                # Importing a submodule can execute locally visible package initializers.
                for count in range(1, len(requested.split(".")) + 1):
                    target = ".".join(requested.split(".")[:count])
                    if target not in modules or (
                        target == module and target != requested
                    ):
                        continue
                    record(
                        kind="local_import_candidate",
                        path=path,
                        line=node.lineno,
                        source=module,
                        target=target,
                    )
                    graph[module].add(target)
    return {
        "schema": "patchshuttle.python_dependencies.v1",
        "analysis": "static_candidates_not_runtime_proof",
        "reference_analysis": "not_performed",
        "dynamic_analysis": "syntactic_hints_only",
        "modules": modules,
        "findings": findings,
        "findings_limit_reached": limited,
        "cycle_groups": cycle_groups(graph),
    }
