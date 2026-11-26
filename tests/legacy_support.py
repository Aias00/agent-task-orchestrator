"""Explicit bootstrap used by offline regressions of the retained kernel."""
import ast
from copy import deepcopy
import hashlib
from pathlib import Path
from unittest.mock import patch

import agent_registry


LEGACY_CLI = Path(__file__).resolve().parent / 'fixtures/legacy_ctask.py'


def legacy_runtime():
    return patch.object(agent_registry, '_runtime_entrypoint', str(LEGACY_CLI))


def core_module_path(root, name):
    return (root if name == 'recovery' else root/'ato_workflow')/(name+'.py')


def imports_builtin_workers(node):
    return (isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == 'ato_adapters'
            and [alias.name for alias in node.names] == ['agents'])


class _LegacyCoreImports(ast.NodeTransformer):
    """Reverse the package relocation for the original core AST fingerprints."""

    def visit_ImportFrom(self, node):
        if imports_builtin_workers(node):
            return ast.Import(names=node.names)
        if node.level == 0 and node.module:
            package, _, module = node.module.partition('.')
            if package in {'ato_common', 'ato_workflow'}:
                if not module:
                    return ast.Import(names=node.names)
                node.module = module
        return node

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and node.func.id == '__import__'
                and len(node.args) == len(node.keywords) == 1
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'ato_workflow.budget'
                and node.keywords[0].arg == 'fromlist'
                and ast.dump(node.keywords[0].value) == ast.dump(ast.parse("['time_enforced']", mode='eval').body)):
            return ast.Call(func=node.func, args=[ast.Constant(value='budget')], keywords=[])
        return self.generic_visit(node)


def legacy_core_digest(tree):
    normalized = _LegacyCoreImports().visit(deepcopy(tree))
    return hashlib.sha256(ast.dump(normalized).encode()).hexdigest()
