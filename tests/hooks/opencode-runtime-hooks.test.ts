// Behavioral test of stratarc/data/hooks/opencode-runtime-hooks.ts, the OpenCode plugin
// bridge (issue #230). Run with `bun test`; scripts/test.py runs it when bun
// is installed and names bun as the missing tool when it is not.
//
// The bridge runs bash scripts from $HOME/.config/opencode/hooks. HOME points
// at a temporary directory before the module is imported, and each hook there
// is a fake that records the payload it was sent and answers as the test says:
// a <name>.deny file makes it print a deny decision (the file's content is the
// reason), <name>.fail makes it exit 1, <name>.garbage makes it print non-JSON.

import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const HOOKS = [
  "tool-budget-guard.sh",
  "prose-guard.sh",
  "no-attribution-guard.sh",
  "env-dump-guard.sh",
  "subagent-cap-guard.sh",
  "reconcile-control-plane.sh",
  "session-start.sh",
]

// Resolved, so a hook's $PWD (macOS reports /private/var for /var) compares equal.
const root = realpathSync(mkdtempSync(join(tmpdir(), "opencode-runtime-hooks-")))
const hookDir = join(root, "home", ".config", "opencode", "hooks")
const logDir = join(root, "logs")
const worktree = join(root, "worktree")
const originalHome = process.env.HOME

type Hooks = {
  event: (input: { event: unknown }) => Promise<void>
  "tool.execute.before": (input: Record<string, unknown>, output: { args: Record<string, unknown> }) => Promise<void>
  "tool.execute.after": (input: Record<string, unknown>) => Promise<void>
}

let bridge: (context: { directory: string; worktree?: string }) => Promise<Hooks>

function fakeHook(name: string): string {
  const base = join(logDir, name)
  return [
    "#!/bin/bash",
    "input=$(cat)",
    `printf '%s\\n' "$input" >> '${base}.log'`,
    `printf '%s\\n' "$PWD" > '${base}.cwd'`,
    `if [ -f '${base}.fail' ]; then exit 1; fi`,
    `if [ -f '${base}.garbage' ]; then echo 'not json'; fi`,
    `if [ -f '${base}.deny' ]; then`,
    `  reason=$(cat '${base}.deny')`,
    `  printf '{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"%s"}}' "$reason"`,
    "fi",
    "",
  ].join("\n")
}

// Every payload a hook received, in order.
function calls(name: string): Record<string, unknown>[] {
  const path = join(logDir, `${name}.log`)
  if (!existsSync(path)) return []
  return readFileSync(path, "utf8").trim().split("\n").map((line) => JSON.parse(line))
}

function called(): string[] {
  return HOOKS.filter((name) => calls(name).length > 0)
}

function answer(name: string, kind: "deny" | "fail" | "garbage", content = ""): void {
  writeFileSync(join(logDir, `${name}.${kind}`), content)
}

beforeAll(async () => {
  process.env.HOME = join(root, "home")
  mkdirSync(worktree, { recursive: true })
  // The bridge reads HOME once, when it is imported.
  bridge = (await import("../../stratarc/data/hooks/opencode-runtime-hooks.ts")).LlmRootHooks
})

afterAll(() => {
  process.env.HOME = originalHome
  rmSync(root, { recursive: true, force: true })
})

beforeEach(() => {
  rmSync(logDir, { recursive: true, force: true })
  mkdirSync(logDir, { recursive: true })
  mkdirSync(hookDir, { recursive: true })
  for (const name of HOOKS) writeFileSync(join(hookDir, name), fakeHook(name))
})

async function hooks(): Promise<Hooks> {
  return bridge({ directory: join(root, "unused-directory"), worktree })
}

describe("tool.execute.before", () => {
  test("a write is normalized to a Write payload for the budget, prose and attribution guards", async () => {
    const h = await hooks()
    await h["tool.execute.before"](
      { tool: "write", sessionID: "s-1", callID: "call-1" },
      { args: { filePath: "/tmp/a.md", content: "hello" } },
    )
    expect(called()).toEqual(["tool-budget-guard.sh", "prose-guard.sh", "no-attribution-guard.sh"])
    expect(calls("tool-budget-guard.sh")[0]).toEqual({
      hook_event_name: "PreToolUse",
      session_id: "s-1",
      cwd: worktree,
      tool_name: "write",
      tool_input: { filePath: "/tmp/a.md", content: "hello" },
      tool_use_id: "call-1",
    })
    const write = {
      hook_event_name: "PreToolUse",
      session_id: "s-1",
      cwd: worktree,
      tool_name: "Write",
      tool_input: { file_path: "/tmp/a.md", content: "hello" },
      tool_use_id: "call-1",
    }
    expect(calls("prose-guard.sh")[0]).toEqual(write)
    expect(calls("no-attribution-guard.sh")[0]).toEqual(write)
  })

  test("an edit carries old and new strings under either spelling, and sessionId is read too", async () => {
    const h = await hooks()
    await h["tool.execute.before"](
      { tool: "edit", sessionId: "s-2" },
      { args: { file_path: "/tmp/b.md", old_string: "x", newString: "y" } },
    )
    const payload = calls("prose-guard.sh")[0]
    expect(payload.session_id).toBe("s-2")
    expect(payload.tool_input).toEqual({ file_path: "/tmp/b.md", old_string: "x", new_string: "y" })
    expect(payload.tool_use_id).toBeUndefined()
  })

  test("an apply_patch is read as its first file and its added lines", async () => {
    const h = await hooks()
    const patchText = [
      "*** Begin Patch",
      "*** Update File: docs/one.md",
      "@@",
      "-old line",
      "+new line",
      "*** Add File: docs/two.md",
      "+second",
      "*** End Patch",
    ].join("\n")
    await h["tool.execute.before"]({ tool: "apply_patch", sessionID: "s" }, { args: { patchText } })
    expect(calls("prose-guard.sh")[0].tool_input).toEqual({ file_path: "docs/one.md", content: "new line\nsecond" })
  })

  test("a bash call reaches only the budget, attribution and environment-dump guards, as Bash", async () => {
    const h = await hooks()
    await h["tool.execute.before"]({ tool: "bash", sessionID: "s" }, { args: { command: "ls" } })
    expect(called()).toEqual(["tool-budget-guard.sh", "no-attribution-guard.sh", "env-dump-guard.sh"])
    for (const name of ["no-attribution-guard.sh", "env-dump-guard.sh"]) {
      const payload = calls(name)[0]
      expect(payload.tool_name).toBe("Bash")
      expect(payload.tool_input).toEqual({ command: "ls" })
    }
  })

  test("an environment-dump deny blocks a bash call with the guard's reason", async () => {
    answer("env-dump-guard.sh", "deny", "Environment dump denied")
    const h = await hooks()
    await expect(
      h["tool.execute.before"]({ tool: "bash", sessionID: "s" }, { args: { command: "x" } }),
    ).rejects.toThrow("Environment dump denied")
  })

  test("a task call reserves a subagent through the cap guard", async () => {
    const h = await hooks()
    await h["tool.execute.before"]({ tool: "task", sessionID: "s", callID: "call-t" }, { args: { subagent_type: "general" } })
    expect(called()).toEqual(["tool-budget-guard.sh", "subagent-cap-guard.sh"])
    const payload = calls("subagent-cap-guard.sh")[0]
    expect([payload.hook_event_name, payload.tool_name, payload.tool_use_id]).toEqual(["PreToolUse", "Task", "call-t"])
  })

  test("any other tool reaches only the budget guard", async () => {
    const h = await hooks()
    await h["tool.execute.before"]({ tool: "read", sessionID: "s" }, { args: { filePath: "/tmp/a" } })
    expect(called()).toEqual(["tool-budget-guard.sh"])
  })

  test("a deny decision blocks the call with the guard's reason and stops later guards", async () => {
    answer("prose-guard.sh", "deny", "no em dash")
    const h = await hooks()
    await expect(
      h["tool.execute.before"]({ tool: "write", sessionID: "s" }, { args: { filePath: "a", content: "b" } }),
    ).rejects.toThrow("no em dash")
    expect(calls("no-attribution-guard.sh")).toEqual([])
  })

  test("a deny decision with no reason blocks with the default message", async () => {
    answer("no-attribution-guard.sh", "deny", "")
    const h = await hooks()
    await expect(h["tool.execute.before"]({ tool: "bash", sessionID: "s" }, { args: { command: "x" } })).rejects.toThrow(
      "Hook denied this action",
    )
  })

  test("a guard that exits non-zero blocks the call", async () => {
    answer("tool-budget-guard.sh", "fail")
    const h = await hooks()
    await expect(h["tool.execute.before"]({ tool: "read", sessionID: "s" }, { args: {} })).rejects.toThrow(
      "tool-budget-guard.sh failed with exit code 1",
    )
  })

  test("output that is not JSON is not a deny", async () => {
    answer("prose-guard.sh", "garbage")
    const h = await hooks()
    await h["tool.execute.before"]({ tool: "write", sessionID: "s" }, { args: { filePath: "a", content: "b" } })
    expect(called()).toContain("no-attribution-guard.sh")
  })

  test("a missing optional hook is skipped but a missing required guard blocks", async () => {
    rmSync(join(hookDir, "tool-budget-guard.sh"))
    const h = await hooks()
    await h["tool.execute.before"]({ tool: "read", sessionID: "s" }, { args: {} })
    rmSync(join(hookDir, "prose-guard.sh"))
    await expect(
      h["tool.execute.before"]({ tool: "write", sessionID: "s" }, { args: { filePath: "a", content: "b" } }),
    ).rejects.toThrow("prose-guard.sh failed with exit code 127")
  })

  test("the hooks run in the worktree, or the directory when there is none", async () => {
    const h = await hooks()
    await h["tool.execute.before"]({ tool: "read", sessionID: "s" }, { args: {} })
    expect(readFileSync(join(logDir, "tool-budget-guard.sh.cwd"), "utf8").trim()).toBe(worktree)
    const directory = join(root, "directory-only")
    mkdirSync(directory, { recursive: true })
    const plain = await bridge({ directory })
    await plain["tool.execute.before"]({ tool: "read", sessionID: "s" }, { args: {} })
    expect(calls("tool-budget-guard.sh")[1].cwd).toBe(directory)
    expect(readFileSync(join(logDir, "tool-budget-guard.sh.cwd"), "utf8").trim()).toBe(directory)
  })
})

describe("tool.execute.after", () => {
  test("an edit reconciles the control plane with a PostToolUse Write payload", async () => {
    const h = await hooks()
    await h["tool.execute.after"]({ tool: "write", sessionID: "s", callID: "c", args: { filePath: "a", content: "b" } })
    expect(called()).toEqual(["reconcile-control-plane.sh"])
    const payload = calls("reconcile-control-plane.sh")[0]
    expect([payload.hook_event_name, payload.tool_name, payload.tool_use_id]).toEqual(["PostToolUse", "Write", "c"])
    expect(payload.tool_input).toEqual({ file_path: "a", content: "b" })
  })

  test("a task call releases its reservation with an empty input", async () => {
    const h = await hooks()
    await h["tool.execute.after"]({ tool: "task", sessionID: "s", callID: "c", args: { prompt: "x" } })
    expect(called()).toEqual(["subagent-cap-guard.sh"])
    const payload = calls("subagent-cap-guard.sh")[0]
    expect([payload.hook_event_name, payload.tool_input, payload.tool_use_id]).toEqual(["PostToolUse", {}, "c"])
  })

  test("any other tool runs nothing afterwards", async () => {
    const h = await hooks()
    await h["tool.execute.after"]({ tool: "bash", sessionID: "s", args: { command: "ls" } })
    expect(called()).toEqual([])
  })
})

describe("event", () => {
  test("a new top-level session starts with a startup SessionStart", async () => {
    const h = await hooks()
    await h.event({ event: { type: "session.created", properties: { info: { id: "top" } } } })
    expect(called()).toEqual(["session-start.sh"])
    expect(calls("session-start.sh")[0]).toEqual({
      hook_event_name: "SessionStart",
      session_id: "top",
      cwd: worktree,
      source: "startup",
    })
  })

  test("a child session confirms a subagent and its idle releases it once", async () => {
    const h = await hooks()
    await h.event({ event: { type: "session.created", properties: { info: { id: "child", parentID: "parent" } } } })
    expect(called()).toEqual(["subagent-cap-guard.sh"])
    await h.event({ event: { type: "session.idle", properties: { sessionID: "child" } } })
    await h.event({ event: { type: "session.idle", properties: { sessionID: "child" } } })
    const sent = calls("subagent-cap-guard.sh").map((p) => [p.hook_event_name, p.session_id, p.agent_id])
    expect(sent).toEqual([
      ["SubagentStart", "parent", "child"],
      ["SubagentStop", "parent", "child"],
    ])
  })

  test("a deleted child names its own parent even when its creation was not seen", async () => {
    const h = await hooks()
    await h.event({ event: { type: "session.deleted", properties: { info: { id: "orphan", parentID: "p" } } } })
    expect(calls("subagent-cap-guard.sh").map((p) => [p.hook_event_name, p.session_id, p.agent_id])).toEqual([
      ["SubagentStop", "p", "orphan"],
    ])
  })

  test("a top-level session going idle and other events run nothing", async () => {
    const h = await hooks()
    await h.event({ event: { type: "session.idle", properties: { sessionID: "top" } } })
    await h.event({ event: { type: "message.updated", properties: {} } })
    expect(called()).toEqual([])
  })

  test("a session start hook the column switched off is skipped", async () => {
    rmSync(join(hookDir, "session-start.sh"))
    const h = await hooks()
    await h.event({ event: { type: "session.created", properties: { info: { id: "top" } } } })
    expect(called()).toEqual([])
  })
})
