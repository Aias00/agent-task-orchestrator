"""Native stream decoders owned by the built-in execution adapters."""


def pi(row):
    kind = row.get('type')
    if kind == 'tool_execution_start':
        return [('start', row.get('toolCallId'), row.get('toolName'), row.get('args', {}))]
    if kind in ('tool_execution_end', 'tool_execution_update'):
        return [('end' if kind.endswith('end') else 'progress', row.get('toolCallId'), None, None)]
    return []


def codex(row):
    kind = row.get('type')
    if kind in ('item.started', 'item.completed'):
        item = row.get('item', {})
        if item.get('type') in ('command_execution', 'mcp_tool_call', 'web_search'):
            name = item.get('tool') or item['type']
            args = item.get('arguments', item.get('command', item.get('query', {})))
            return [('start' if kind.endswith('started') else 'end', item.get('id'), name, args)]
    return []


def claude(row):
    if row.get('type') not in ('assistant', 'user'):
        return []
    result = []
    for block in row.get('message', {}).get('content', []):
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'tool_use':
            result.append(('start', block.get('id'), block.get('name'), block.get('input', {})))
        elif block.get('type') == 'tool_result':
            result.append(('end', block.get('tool_use_id'), None, None))
    return result


def legacy(row):
    return codex(row) or claude(row) or pi(row)
