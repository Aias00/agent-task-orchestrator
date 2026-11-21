#!/usr/bin/env python3
"""Generate an isolated, version-pinned pi-acp receipt adapter; never install it.

This program only transforms the installed source and creates local symlinks
to its existing dependencies. It does not start Pi, read credentials, or edit
the installed package. See docs/acp-process-migration.md before using it.
"""
import argparse
import hashlib
import json
from pathlib import Path

SOURCE = Path('OPERATOR_DEPENDENCIES/acp-route-validation-20261118/node_modules/pi-acp/dist/index.js')
SHA256 = '324aeb8bba1228937e16b1326fb3e014e2a625b2dea549dbcd49b006cd5df6a2'

def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError('upstream source does not match pinned patch')
    return source.replace(before, after, 1)

def derive(source):
    source = replace_once(source,
        '  return join(homedir(), ".pi", "pi-acp");',
        '  if (!process.env.CTASK_PI_ACP_STATE_DIR) throw new Error("isolated state directory required");\n  return process.env.CTASK_PI_ACP_STATE_DIR;')
    source = replace_once(source, 'const updateNotice = buildUpdateNotice();',
                          'const updateNotice = null; // No npm/network version probe in this profile.')
    source = replace_once(source,
        '  async prompt(message, images = []) {\n    const expandedMessage',
        '  async prompt(message, images = [], ctaskTurn) {\n    const expandedMessage')
    source = replace_once(source,
        'const queued = { message: expandedMessage, images, resolve: resolve4, reject };',
        'const queued = { message: expandedMessage, images, ctaskTurn, resolve: resolve4, reject };')
    source = replace_once(source,
        '    this.cancelRequested = false;\n    this.inAgentLoop = false;\n    this.pendingTurn = { resolve: t.resolve, reject: t.reject };',
        '    this.cancelRequested = false;\n    this.inAgentLoop = false;\n    this.ctaskTurn = t.ctaskTurn;\n    this.ctaskNativeError = false;\n    this.ctaskNativeTerminal = null;\n    this.ctaskNativeResult = null;\n    this.pendingTurn = { resolve: t.resolve, reject: t.reject };')
    source = replace_once(source,
        '  async settleTurn() {\n    await this.publishContextUsage();',
        '  async settleTurn() {\n    if (this.ctaskNativeError) this.ctaskReceipt("error");\n    else if (this.ctaskNativeTerminal === "stop") this.ctaskReceipt("stop");\n    await this.publishContextUsage();')
    source = replace_once(source,
        '    this.proc.prompt(t.message, t.images).catch((err) => {',
        '    this.proc.prompt(t.message, t.images).catch((err) => {\n      this.ctaskNativeError = true;\n      this.ctaskReceipt("error");')
    source = replace_once(source,
        '  handlePiEvent(ev) {\n    const type = String(ev.type ?? "");',
        '''  ctaskReceipt(reason) {
    if (this.pendingTurn && typeof this.ctaskTurn === "string") {
      this.emit({sessionUpdate: "session_info_update", _meta: {
        ctaskNativeReceipt: {turn: this.ctaskTurn, stopReason: reason,
          ...(reason === "stop" && this.ctaskNativeResult !== null ? {result: this.ctaskNativeResult} : {})}
      }});
    }
  }
  handlePiEvent(ev) {
    if (ev.type === "message_end" && ev.message?.role === "assistant" && this.pendingTurn) {
      const reason = ev.message.stopReason;
      if (["error", "aborted", "cancelled"].includes(reason)) {
        this.ctaskNativeError = true;
      }
      this.ctaskNativeTerminal = reason;
      this.ctaskNativeResult = null;
      if (reason === "stop" && Array.isArray(ev.message.content)) {
        const parts = ev.message.content.filter(x => x?.type === "text" && typeof x.text === "string");
        const result = parts.map(x => x.text).join("");
        if (result.length <= 1048576) this.ctaskNativeResult = result;
      }
    }
    const type = String(ev.type ?? "");''')
    source = replace_once(source,
        'const result = await session.prompt(message, images);',
        'const result = await session.prompt(message, images, params._meta?.ctaskTurn);')
    return source

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    raw = SOURCE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError('installed pi-acp source hash differs from pinned version')
    package = json.loads((SOURCE.parents[1]/'package.json').read_text())
    if package.get('name') != 'pi-acp' or package.get('version') != '0.0.34':
        raise ValueError('installed pi-acp package version differs from pinned version')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output/'pi-acp.mjs').write_text(derive(raw.decode()))
    (output/'package.json').write_text(json.dumps({'name': 'pi-acp', 'version': '0.0.34', 'type': 'module'})+'\n')
    modules = output/'node_modules'
    modules.mkdir()
    (modules/'@agentclientprotocol').symlink_to(SOURCE.parents[1]/'node_modules/@agentclientprotocol', target_is_directory=True)
    (modules/'cross-spawn').symlink_to(SOURCE.parents[2]/'cross-spawn', target_is_directory=True)
    print(hashlib.sha256((output/'pi-acp.mjs').read_bytes()).hexdigest())

if __name__ == '__main__':
    main()
