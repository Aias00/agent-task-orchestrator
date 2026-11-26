#!/usr/bin/env python3
"""Read-only static dependency report, including delayed imports and runtime packages."""
import ast
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime_sources(root=ROOT):
    yield from root.glob('*.py')
    for marker in root.glob('ato_*/__init__.py'):
        yield from marker.parent.rglob('*.py')


def module_graph(root=ROOT):
    sources = {}
    for path in runtime_sources(root):
        parts = list(path.relative_to(root).with_suffix('').parts)
        if parts[-1] == '__init__':
            parts.pop()
        sources['.'.join(parts)] = path
    graph = {}
    for name, path in sources.items():
        package = name if path.name == '__init__.py' else name.rpartition('.')[0]
        dependencies = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                candidates = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = (importlib.util.resolve_name('.'*node.level+(node.module or ''), package)
                        if node.level else node.module or '')
                candidates = [base+'.'+alias.name if base+'.'+alias.name in sources else base
                              for alias in node.names]
            else:
                continue
            dependencies.update(candidate for candidate in candidates if candidate in sources)
        graph[name] = sorted(dependencies)
    return dict(sorted(graph.items()))


def mutual_pairs(graph):
    return sorted([left, right] for left, edges in graph.items() for right in edges
                  if left < right and left in graph[right])


def report(root=ROOT):
    graph = module_graph(root)
    roots = {path.stem for path in root.glob('*.py')}
    root_graph = {name: [edge for edge in graph[name] if edge in roots] for name in sorted(roots)}
    return dict(root_module_count=len(roots), runtime_module_count=len(graph),
        root_mutual_count=len(mutual_pairs(root_graph)), root_mutual_pairs=mutual_pairs(root_graph),
        runtime_mutual_count=len(mutual_pairs(graph)), runtime_mutual_pairs=mutual_pairs(graph), graph=graph)


if __name__ == '__main__':
    result = report()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['runtime_mutual_count'] <= 8 else 1)
